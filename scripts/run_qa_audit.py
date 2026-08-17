#!/usr/bin/env python3
"""Audit both the expert gold data and our own predictions.

Two populations, deliberately:

  gold        - the expert phenopackets. Finding violations here validates the
                checker and produces feedback worth sending upstream.
  predictions - our baseline output. This is the measured error rate that goes
                into docs/ERROR_LEDGER.md.

Writes reports/qa_audit.json.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.evalset import build_eval_papers
from rdcd.extract.baseline import DictionaryExtractor
from rdcd.ontology.store import STORE
from rdcd.qa import constraints
from rdcd.qa.distant import DistantSupervisor
from rdcd.qa.provenance import ProvenanceVerifier
from rdcd.qa.retractions import RetractionIndex

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    papers = build_eval_papers(include_retracted=True)
    print(f"auditing {len(papers)} papers")

    # --- gold-set coherence -------------------------------------------------
    gold = [c for p in papers for c in p.gold_cases]
    gold_violations = constraints.check_many(STORE, gold)
    print(f"gold cases: {len(gold)}  violations: {gold_violations}")

    # --- our own predictions ------------------------------------------------
    ex = DictionaryExtractor(STORE)
    pv = ProvenanceVerifier(STORE)
    ds = DistantSupervisor(STORE)
    verdicts, pred_violations = [], Counter()
    distant = Counter()
    n_pred = 0
    for i, p in enumerate(papers, 1):
        if not p.pmcid or not ncbi.cached("pmcxml", f"pmcxml:{p.pmcid}", "xml"):
            continue
        doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
        if not doc.has_body:
            continue
        rec = ex.extract(doc, p.source_doc())[0]
        n_pred += 1
        verdicts.extend(pv.verify(doc, rec))
        for v in constraints.check(STORE, rec):
            pred_violations[f"{v.severity}:{v.code}"] += 1
        distant[str(ds.check(doc, rec).agrees)] += 1
        if i % 200 == 0:
            print(f"  {i}/{len(papers)}", flush=True)

    prov = pv.rates(verdicts)

    # --- retractions --------------------------------------------------------
    ri = RetractionIndex()
    retracted = {}
    for p in papers:
        n = ri.check(pmid=p.pmid)
        if n:
            retracted[p.pmid] = n.summary()

    out = {
        "gold": {"n_cases": len(gold), "violations": gold_violations},
        "predictions": {
            "n_records": n_pred,
            "provenance": prov,
            "violations": dict(pred_violations.most_common()),
            "distant_supervision_agreement": dict(distant),
        },
        "retractions": {
            "retraction_watch_notices_indexed": len(ri.rw),
            "eval_papers_retracted": retracted,
        },
    }
    (ROOT / "reports" / "qa_audit.json").write_text(json.dumps(out, indent=1))
    print("\n=== provenance (predictions) ===")
    for k, v in prov.items():
        print(f"  {k}: {v}")
    print("=== constraint violations (predictions) ===")
    for k, v in out["predictions"]["violations"].items():
        print(f"  {k}: {v}")
    print("=== distant supervision ===", dict(distant))
    print("=== retracted eval papers ===", retracted)
    print("\nwrote reports/qa_audit.json")


if __name__ == "__main__":
    main()
