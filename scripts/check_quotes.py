#!/usr/bin/env python3
"""Verify every quote in a manual extraction is a character-for-character substring.

This checks compliance with the one hard rule the extraction prompt states. It does
not consult the gold set and does not report accuracy, so running it cannot leak the
answers or tune the extraction toward them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "work" / "manual_extraction"


def check(pmid: str) -> tuple[int, int]:
    text = (WORK / f"{pmid}.txt").read_text()
    data = json.loads((WORK / f"{pmid}.json").read_text())
    ok = bad = 0
    for ind in data.get("individuals", []):
        quotes = [f["quote"] for f in ind.get("findings", [])]
        for extra in ("diagnosis_quote", "gene_symbol", "hgvs_c", "hgvs_p"):
            if ind.get(extra):
                quotes.append(ind[extra])
        for q in quotes:
            if q and q in text:
                ok += 1
            elif q:
                bad += 1
                print(f"  {pmid}: NOT A SUBSTRING: {q!r}")
    return ok, bad


def main() -> None:
    pmids = sys.argv[1:] or sorted(p.stem for p in WORK.glob("*.json") if p.stem[0].isdigit())
    t_ok = t_bad = 0
    for pmid in pmids:
        if not (WORK / f"{pmid}.json").exists():
            continue
        ok, bad = check(pmid)
        t_ok += ok
        t_bad += bad
    print(f"\n{t_ok} quotes verified, {t_bad} not substrings")
    sys.exit(1 if t_bad else 0)


if __name__ == "__main__":
    main()
