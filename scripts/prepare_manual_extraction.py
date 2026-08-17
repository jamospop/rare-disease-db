#!/usr/bin/env python3
"""Write out document text for extraction by an LLM that has no API access.

The point: Claude Code (or any assistant with filesystem access, or a local model
behind an OpenAI-compatible endpoint) can act as the extractor without an API key.
It reads the .txt files this writes, and writes a .json per paper in exactly the
shape `rdcd.extract.llm`'s tool schema defines. `score_manual_extraction.py` then
grounds and scores that JSON through the *same* code path as the API extractor.

Validity is structural, not a promise: this script writes **document text only**.
No gold phenotypes, no diagnoses, no gene answers are placed anywhere the extractor
can see. The scorer is a separate invocation that reads the gold set for the first
time. An extractor that peeked would have to go looking outside its work directory.

    make manual-prep          # writes work/manual_extraction/
    <extractor writes NNNN.json alongside each NNNN.txt>
    make manual-score         # grounds + scores, offline
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.evalset import build_eval_papers
from rdcd.extract.llm import CASES_SCHEMA, SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "manual_extraction"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", default="single")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start", type=int, default=0, help="Skip the first N (for batching)")
    args = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "_INSTRUCTIONS.md").write_text(
        "# Extraction task\n\n"
        "For each `<pmid>.txt` in this directory, write `<pmid>.json` matching the schema\n"
        "in `_SCHEMA.json`. The instructions the API extractor receives are in\n"
        "`_SYSTEM_PROMPT.txt` and apply identically here.\n\n"
        "The single hardest rule: **every `quote` must be a character-for-character\n"
        "substring of the .txt file.** Quotes that are not are dropped by the scorer, and\n"
        "the finding is lost. Do not normalise spelling, expand abbreviations, or fix\n"
        "hyphenation.\n\n"
        "Do not look at the gold set. It is not in this directory, and reading it would\n"
        "invalidate the measurement.\n"
    )
    (WORK / "_SCHEMA.json").write_text(json.dumps(CASES_SCHEMA, indent=1))
    (WORK / "_SYSTEM_PROMPT.txt").write_text(SYSTEM_PROMPT)

    papers, n = [], 0
    for p in build_eval_papers():
        if p.track != args.track or p.split != args.split:
            continue
        if not p.pmcid or not ncbi.cached("pmcxml", f"pmcxml:{p.pmcid}", "xml"):
            continue
        doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
        if not doc.has_body:
            continue
        n += 1
        if n <= args.start:
            continue
        (WORK / f"{p.pmid}.txt").write_text(doc.text)
        papers.append({"pmid": p.pmid, "pmcid": p.pmcid, "chars": len(doc.text),
                       "tier": p.tier, "quotes_permitted": p.quotes_permitted})
        if args.limit and len(papers) >= args.limit:
            break

    (WORK / "_MANIFEST.json").write_text(json.dumps(
        {"track": args.track, "split": args.split, "start": args.start,
         "papers": papers}, indent=1))
    total = sum(p["chars"] for p in papers)
    print(f"wrote {len(papers)} documents to {WORK}")
    print(f"  total {total:,} chars (~{total//4:,} tokens)")
    print(f"  mean {total//max(1,len(papers)):,} chars per paper")
    print(f"\nNext: write <pmid>.json for each, then: make manual-score")


if __name__ == "__main__":
    main()
