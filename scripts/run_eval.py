#!/usr/bin/env python3
"""Score an extractor on the eval set. Offline; reads only cached full text."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.eval.harness import format_summary, run_extractor, write_report
from rdcd.extract.baseline import BaselineConfig, DictionaryExtractor
from rdcd.ontology.store import STORE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", default="dictionary", choices=["dictionary"])
    ap.add_argument("--splits", default="dev,test")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--all-sections", action="store_true",
                    help="Disable the patient-section filter (ablation)")
    ap.add_argument("--no-related-synonyms", action="store_true",
                    help="Drop multi-word BROAD/RELATED synonyms (ablation)")
    ap.add_argument("--no-phenotype-root", action="store_true",
                    help="Ground against all of HPO, not just Phenotypic abnormality (ablation)")
    args = ap.parse_args()

    cfg = BaselineConfig(
        patient_sections_only=not args.all_sections,
        multiword_related_synonyms=not args.no_related_synonyms,
        restrict_to_phenotypic_abnormality=not args.no_phenotype_root,
    )
    ex = DictionaryExtractor(STORE, cfg)
    res = run_extractor(STORE, ex, splits=tuple(args.splits.split(",")), limit=args.limit)
    rep = res.report()
    print(format_summary(rep))
    print("\nwrote", write_report(res, args.name))


if __name__ == "__main__":
    main()
