#!/usr/bin/env python3
"""Warm the cache with JATS full text for every eval paper.

Separate from the harness on purpose: fetching is slow and rate-limited, scoring
should be instant and offline. After this runs, `make eval` touches no network.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.evalset import build_eval_papers


def main() -> None:
    papers = build_eval_papers()
    print(f"fetching full text for {len(papers)} eval papers")
    stats = Counter()
    for i, p in enumerate(papers, 1):
        if not p.pmcid:
            stats["no_pmcid"] += 1
            continue
        try:
            xml = ncbi.pmc_fulltext_xml(p.pmcid)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {p.pmid}/{p.pmcid}: {type(e).__name__}: {e}")
            stats["fetch_error"] += 1
            continue
        doc = parse_jats(xml)
        stats["ok" if doc.has_body else "no_body"] += 1
        stats["chars"] += len(doc.text)
        if i % 100 == 0:
            print(f"  {i}/{len(papers)}  {dict(stats)}")
    print("done:", dict(stats))
    n = stats["ok"] or 1
    print(f"mean chars per paper with body: {stats['chars'] // n:,}")


if __name__ == "__main__":
    main()
