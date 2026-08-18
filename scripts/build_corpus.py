#!/usr/bin/env python3
"""Extract structured cases from papers nobody has curated.

Why this exists: every record shipped so far came from a paper that is already in
GA4GH phenopacket-store, i.e. already curated by experts. That is useful for
measuring extraction, and it adds exactly nothing to the world's stock of
structured rare-disease data. This pass targets the ~442,000 open-access case
reports in PMC that no structured resource covers, and **excludes** every paper in
the gold set so the output is net-new by construction.

Quality is the dictionary baseline's: phenotype graded F1 0.56, absent-finding F1
0.11 (docs/BENCHMARKS.md). That is a discovery layer, not a diagnostic one, and the
datasheet says so. It is still the difference between a case being findable and
being buried in prose.

    make corpus            # discover, fetch, extract  (network, rate-limited)
    make corpus-release    # package what was extracted
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.goldsets import group_by_pmid, load_gold
from rdcd.extract.baseline import DictionaryExtractor
from rdcd.ontology.store import STORE
from rdcd.qa import constraints
from rdcd.qa.retractions import RetractionIndex
from rdcd.schema import SourceDoc

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "corpus"

# Chosen for usefulness rather than volume: open-access case reports that report a
# genetic or syndromic finding, which is where structured phenotype data helps.
# Tightened after inspecting what the first pass actually returned: ~8% of it was
# reviews, editorials and even fungal taxonomy papers. Those contain no patients, so
# "extracting cases" from them manufactures records with no referent - the worst
# possible failure for a corpus people are meant to search. PMC's "case reports"[pt]
# filter alone is too permissive; excluding review types cuts the pool from 19,850 to
# 3,019 and removes nearly all of it.
QUERY = (
    '"case reports"[pt] AND "open access"[filter] AND '
    '(mutation[tiab] OR variant[tiab]) AND 2012:2026[dp] '
    'NOT review[pt] NOT "systematic review"[ti]'
)

# Second line of defence, independent of the query: titles that announce a review.
REVIEWISH = re.compile(
    r"\b(review|meta-analysis|systematic|scoping|narrative)\b"
    r"|current (status|approaches|perspectives|concepts)"
    r"|\bstate of the art\b|\bupdate on\b|\badvances in\b"
    r"|\boverview of\b|\bwhat we know\b",
    re.I,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--query", default=QUERY)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    print("discovering candidates ...", flush=True)
    pmcids = ncbi.esearch(args.query, db="pmc", retmax=args.limit * 2)
    print(f"  {len(pmcids)} PMC ids from the query")

    gold_pmids = set(group_by_pmid(load_gold()))
    print(f"  excluding {len(gold_pmids)} already-curated gold-set papers")

    ex = DictionaryExtractor(STORE)
    ri = RetractionIndex()
    stats = Counter()
    t0 = time.time()
    records_path = OUT / "cases.jsonl"
    sources_path = OUT / "sources.jsonl"

    seen_pmids: set[str] = set()
    with records_path.open("w") as fc, sources_path.open("w") as fs:
        for i, raw in enumerate(pmcids, 1):
            if stats["kept"] >= args.limit:
                break
            pmcid = raw if str(raw).startswith("PMC") else f"PMC{raw}"
            try:
                oa = ncbi.oa_status(pmcid)
            except Exception:  # noqa: BLE001
                stats["oa_error"] += 1
                continue
            if not oa.in_oa_subset:
                stats["not_oa"] += 1
                continue
            try:
                doc = parse_jats(ncbi.pmc_fulltext_xml(pmcid))
            except Exception:  # noqa: BLE001
                stats["fetch_error"] += 1
                continue
            if not doc.has_body:
                stats["no_body"] += 1
                continue
            if doc.title and REVIEWISH.search(doc.title):
                stats["looks_like_review"] += 1
                continue
            pmid = doc.pmid
            if pmid and pmid in gold_pmids:
                stats["already_curated"] += 1
                continue
            if pmid and pmid in seen_pmids:
                stats["duplicate"] += 1
                continue
            if pmid:
                seen_pmids.add(pmid)

            notice = ri.check(pmid=pmid, doi=doc.doi) if (pmid or doc.doi) else None
            src = SourceDoc(
                pmid=pmid, pmcid=pmcid, doi=doc.doi, title=doc.title,
                journal=doc.journal, year=doc.year,
                license=oa.license, in_oa_subset=True,
                quotes_permitted=oa.redistributable,
                retracted=bool(notice) or oa.retracted,
                retraction_notice=notice.summary() if notice else None,
            )
            rec = ex.extract(doc, src)[0]
            rec, dropped = rec.enforce_provenance()
            stats["dropped_unprovenanced"] += len(dropped)
            if not rec.phenotypes:
                stats["no_phenotypes"] += 1
                continue
            rec = constraints.annotate(STORE, rec)
            stats["kept"] += 1
            stats["assertions"] += len(rec.phenotypes) + len(rec.diagnoses) + len(rec.variants)
            if src.retracted:
                stats["retracted"] += 1
            for f in rec.qa_flags:
                stats[f"flag:{f}"] += 1
            fc.write(rec.model_dump_json(exclude_none=True) + "\n")
            fs.write(json.dumps(src.model_dump(exclude_none=True)) + "\n")

            if i % 250 == 0:
                el = time.time() - t0
                print(f"  {i}/{len(pmcids)} scanned, {stats['kept']} kept, "
                      f"{el/60:.1f} min", flush=True)

    summary = {
        "query": args.query,
        "scanned": len(pmcids),
        "stats": dict(stats.most_common()),
        "elapsed_min": round((time.time() - t0) / 60, 1),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + json.dumps(summary, indent=1))
    print(f"\nwrote {records_path}")


if __name__ == "__main__":
    main()
