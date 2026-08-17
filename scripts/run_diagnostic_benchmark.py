#!/usr/bin/env python3
"""The §6 proof: what does extraction error cost diagnostic ranking?

Two conditions over the same papers, same ranker, same target:
  ceiling  - query built from the expert gold phenotypes
  pipeline - query built from our extracted phenotypes

Reports top-k recall and MRR for both. The gap is the answer to "should the next
month go into extraction or into the ranker?".
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.diagnose import PhenotypeRanker
from rdcd.eval.evalset import build_eval_papers
from rdcd.eval.metrics import mean_reciprocal_rank, topk_recall
from rdcd.extract.baseline import BaselineConfig, DictionaryExtractor
from rdcd.schema import Section
from rdcd.ontology.store import STORE

ROOT = Path(__file__).resolve().parents[1]
KS = (1, 3, 5, 10, 20)
TOP = 100


def main() -> None:
    track = sys.argv[1] if len(sys.argv) > 1 else "single"
    # Optional 2nd arg: "abstract-only" simulates the abstract_only licence tier,
    # answering whether data we ARE allowed to extract from non-OA papers still
    # supports diagnosis. See docs/BENCHMARKS.md.
    mode = sys.argv[2] if len(sys.argv) > 2 else "fulltext"
    papers = [p for p in build_eval_papers() if p.track == track]
    ranker = PhenotypeRanker(STORE)
    cfg = BaselineConfig(sections=(Section.TITLE, Section.ABSTRACT)) \
        if mode == "abstract-only" else BaselineConfig()
    ex = DictionaryExtractor(STORE, cfg)

    agg = {c: {"hits": Counter(), "mrr": 0.0, "n": 0, "no_query": 0, "unmappable_target": 0}
           for c in ("ceiling", "pipeline")}
    per_case = []

    for i, p in enumerate(papers, 1):
        if not p.pmcid or not ncbi.cached("pmcxml", f"pmcxml:{p.pmcid}", "xml"):
            continue
        doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
        if not doc.has_body:
            continue
        gold = p.gold_cases[0] if track == "single" else p.union_gold
        targets = {d for d in (STORE.normalize_disease(x) for x in gold.disease_ids) if d}
        if not targets:
            for c in agg:
                agg[c]["unmappable_target"] += 1
            continue
        pred = ex.extract(doc, p.source_doc())[0]
        queries = {"ceiling": sorted(gold.observed_hpo), "pipeline": sorted(pred.observed_hpo)}
        row = {"pmid": p.pmid, "split": p.split, "targets": sorted(targets)}
        for cond, q in queries.items():
            if not q:
                agg[cond]["no_query"] += 1
                row[cond] = {"n_query": 0, "rank": None}
                continue
            ranked = [r.disease_id for r in ranker.rank(q, top=TOP)]
            hits = topk_recall(ranked, targets, KS)
            mrr = mean_reciprocal_rank(ranked, targets)
            agg[cond]["n"] += 1
            agg[cond]["mrr"] += mrr
            for k, v in hits.items():
                agg[cond]["hits"][k] += v
            rank = next((j for j, d in enumerate(ranked, 1) if d in targets), None)
            row[cond] = {"n_query": len(q), "rank": rank}
        per_case.append(row)
        if i % 25 == 0:
            print(f"  {i}/{len(papers)} papers", flush=True)

    out = {"track": track, "mode": mode, "n_papers": len(per_case),
           "top_k": list(KS), "conditions": {}}
    for cond, a in agg.items():
        n = a["n"] or 1
        out["conditions"][cond] = {
            "n_scored": a["n"],
            "no_query": a["no_query"],
            "unmappable_target": a["unmappable_target"],
            "topk_recall": {str(k): round(a["hits"][k] / n, 4) for k in KS},
            "mrr": round(a["mrr"] / n, 4),
        }
    ce, pi = out["conditions"]["ceiling"], out["conditions"]["pipeline"]
    out["extraction_cost"] = {
        f"top{k}_absolute_drop": round(ce["topk_recall"][str(k)] - pi["topk_recall"][str(k)], 4)
        for k in KS
    }
    out["extraction_cost"]["mrr_drop"] = round(ce["mrr"] - pi["mrr"], 4)
    suffix = track if mode == "fulltext" else f"{track}_{mode}"
    (ROOT / "reports" / f"diagnostic_benchmark_{suffix}.json").write_text(
        json.dumps({"summary": out, "per_case": per_case}, indent=1))

    print(f"\ntrack={track} mode={mode}  papers scored={out['n_papers']}")
    print(f"{'condition':10} {'n':>4} " + " ".join(f"top{k:<3}" for k in KS) + "   MRR")
    for cond in ("ceiling", "pipeline"):
        c = out["conditions"][cond]
        print(f"{cond:10} {c['n_scored']:>4} " +
              " ".join(f"{c['topk_recall'][str(k)]:.3f}" for k in KS) + f"  {c['mrr']:.3f}")
    print("cost of extraction error:", json.dumps(out["extraction_cost"]))


if __name__ == "__main__":
    main()
