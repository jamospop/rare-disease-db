#!/usr/bin/env python3
"""Build a versioned dataset release.

Emits, into data/releases/<version>/:
  cases.jsonl          one CaseRecord per line (schema v1, with provenance)
  phenopackets.jsonl   the same records as GA4GH Phenopackets v2
  sources.jsonl        per-source licence tier and retraction status
  DATASHEET.md         what this is, how it was made, what is wrong with it
  SHA256SUMS           checksums for every file above
  manifest.json        counts, provenance, git revision, input versions

Built from the eval-set extractions, so it is small by design: the point is that
the release machinery, licence tiering, and checksums work end to end before a
corpus-scale run exists. A release that cannot be verified is not a release.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.evalset import build_eval_papers
from rdcd.extract.baseline import DictionaryExtractor
from rdcd.ontology.store import STORE
from rdcd.qa import constraints
from rdcd.qa.retractions import RetractionIndex

ROOT = Path(__file__).resolve().parents[1]
VERSION = f"0.1.0-dev+{date.today().isoformat()}"


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip() or "uncommitted"
    except Exception:  # noqa: BLE001
        return "unknown"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    outdir = ROOT / "data" / "releases" / VERSION
    outdir.mkdir(parents=True, exist_ok=True)
    papers = build_eval_papers()
    ex = DictionaryExtractor(STORE)
    ri = RetractionIndex()

    tiers: Counter = Counter()
    flags: Counter = Counter()
    n_cases = n_assertions = 0

    with (outdir / "cases.jsonl").open("w") as fc, \
         (outdir / "phenopackets.jsonl").open("w") as fp, \
         (outdir / "sources.jsonl").open("w") as fs:
        for i, p in enumerate(papers, 1):
            if not p.pmcid or not ncbi.cached("pmcxml", f"pmcxml:{p.pmcid}", "xml"):
                continue
            doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
            if not doc.has_body:
                continue
            src = p.source_doc()
            notice = ri.check(pmid=p.pmid)
            if notice:
                src = src.model_copy(update={"retracted": True,
                                             "retraction_notice": notice.summary()})
            rec = ex.extract(doc, src)[0]
            rec, _ = rec.enforce_provenance()
            rec = constraints.annotate(STORE, rec)
            tiers[p.tier] += 1
            for f in rec.qa_flags:
                flags[f] += 1
            n_cases += 1
            n_assertions += len(rec.phenotypes) + len(rec.diagnoses) + len(rec.variants)
            fc.write(rec.model_dump_json(exclude_none=True) + "\n")
            fp.write(json.dumps(rec.to_phenopacket()) + "\n")
            fs.write(json.dumps(src.model_dump(exclude_none=True)) + "\n")
            if i % 200 == 0:
                print(f"  {i}/{len(papers)}", flush=True)

    manifest = {
        "name": "open-rare-disease-case-database",
        "version": VERSION,
        "built": date.today().isoformat(),
        "schema_version": "1.0.0",
        "git_rev": git_rev(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "extractor": ex.name,
        "counts": {
            "records": n_cases,
            "assertions": n_assertions,
            "by_licence_tier": dict(tiers),
            "qa_flags": dict(flags.most_common()),
        },
        "inputs": {
            "phenopacket_store": "0.1.27",
            "hpo": "hp.obo (fetched by make data)",
            "mondo": "mondo.obo (fetched by make data)",
            "hgnc": "hgnc_complete_set.txt",
            "hpoa": "phenotype.hpoa",
            "retraction_watch_notices": len(ri.rw),
        },
        "licence": {"code": "Apache-2.0", "data": "CC-BY-4.0"},
        "caveat": (
            "Built with the dictionary baseline (graded phenotype F1 0.56 on Track SINGLE; "
            "absent-finding F1 0.11). "
            "Not fit for clinical use. See docs/ERROR_LEDGER.md."
        ),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (outdir / "DATASHEET.md").write_text(datasheet(manifest, tiers))

    sums = outdir / "SHA256SUMS"
    sums.write_text("".join(
        f"{sha256(f)}  {f.name}\n" for f in sorted(outdir.iterdir()) if f.name != "SHA256SUMS"
    ))

    print(f"\nrelease {VERSION}")
    print(f"  {n_cases} records, {n_assertions} assertions")
    print(f"  tiers: {dict(tiers)}")
    print(f"  -> {outdir}")
    for f in sorted(outdir.iterdir()):
        print(f"     {f.name:22} {f.stat().st_size / 1e6:7.2f} MB")


def datasheet(m: dict, tiers: Counter) -> str:
    return f"""# Datasheet — {m['name']} {m['version']}

## What this is
Individual-level rare-disease case records mined from the published literature, in a
GA4GH-Phenopacket-compatible schema where every asserted fact carries a character offset
into its source document.

- Records: **{m['counts']['records']}**
- Assertions: **{m['counts']['assertions']}**
- Schema version: {m['schema_version']}
- Extractor: `{m['extractor']}`
- Built: {m['built']} (git {m['git_rev']})

## How it was made
Source papers are the GA4GH phenopacket-store 0.1.27 gold set restricted to the PMC Open
Access subset. Full text was fetched via PMC E-utilities, parsed from JATS into normalised
text with stable offsets, then extracted with the dictionary + negation baseline. Every
assertion without provenance was dropped; every record was checked against machine-checkable
ontology constraints and ships with its flags.

## Licence tiers
{chr(10).join(f'- `{k}`: {v} records' for k, v in sorted(tiers.items()))}

`Evidence.quote` is populated only for sources whose licence permits redistributing
expression. Character offsets are present for all records regardless of tier — an offset into
a public document is a fact about that document. See docs/LICENSING.md.

## Known limitations
{m['caveat']}

Specifically: absent-phenotype recall is poor (F1 0.11); the baseline does not segment
individuals, so records are document-level and can contain polarity contradictions across
individuals; diagnosis extraction is weak (F1 0.25). Full measured error rates with rates and
status: docs/ERROR_LEDGER.md.

## Intended use
Research on literature-scale phenotype extraction, and as a reproducible baseline. **Not for
clinical decision-making.** Cite the source publication, not this dataset, when the claim is
the paper's.

## Files
- `cases.jsonl` — one record per line, schema v1
- `phenopackets.jsonl` — the same records as GA4GH Phenopackets v2
- `sources.jsonl` — per-source identifiers, licence, tier, retraction status
- `manifest.json` — counts, input versions, build environment
- `SHA256SUMS` — checksums for every file above
"""


if __name__ == "__main__":
    main()
