#!/usr/bin/env python3
"""Measure the scoreable eval set: gold cases whose source text we may read.

Writes reports/goldset_availability.json. This is the number that sizes the
whole benchmark, so it is a committed artifact, not a console print.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.eval.goldsets import audit_availability, group_by_pmid, load_gold, store_version

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "goldset_availability.json"


def main() -> None:
    gold = load_gold()
    by_pmid = group_by_pmid(gold)
    print(f"phenopacket-store {store_version()}: {len(gold)} cases / {len(by_pmid)} PMIDs")
    av = audit_availability(sorted(by_pmid))
    for pmid, a in av.items():
        a.n_cases = len(by_pmid.get(pmid, []))

    tiers = Counter(a.tier for a in av.values())
    cases_by_tier = Counter()
    for a in av.values():
        cases_by_tier[a.tier] += a.n_cases
    lic = Counter((a.license or "none") for a in av.values() if a.in_oa_subset)
    retracted = [a.pmid for a in av.values() if a.retracted]

    summary = {
        "store_version": store_version(),
        "gold_cases": len(gold),
        "gold_pmids": len(by_pmid),
        "papers_by_tier": dict(tiers),
        "cases_by_tier": dict(cases_by_tier),
        "oa_licenses": dict(lic.most_common()),
        "retracted_pmids": retracted,
        "in_pmc_papers": sum(1 for a in av.values() if a.in_pmc),
        "in_oa_papers": sum(1 for a in av.values() if a.in_oa_subset),
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary,
                               "per_paper": {k: v.to_dict() for k, v in sorted(av.items())}},
                              indent=1))
    print(json.dumps(summary, indent=1))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
