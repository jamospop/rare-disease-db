#!/usr/bin/env python3
"""Run the LLM extractor on the dev split and score it against the same gold data.

This is the project's go/no-go run: it converts the LLM extractor from scaffolding
into a measurement. Compare its Track SINGLE / dev graded F1 against the dictionary
baseline's 0.5597 and the plan's 0.85 target.

    make llm-dev-dry      # cost + token estimate, no API calls
    make llm-dev          # synchronous run (fast feedback, full price)
    make llm-dev-batch    # Batch API (50% cheaper, up to 24h)

The API key is read from the environment, or from a gitignored `.env` in the repo
root (`ANTHROPIC_API_KEY=sk-ant-...`), so it never has to be pasted into a shell
that logs history.

Reports the three grounding statistics BEFORE any F1, because they decide how to
read the F1:
  quote_not_found     -> the model's hallucination rate (fabricated quotes, dropped)
  quote_ungroundable  -> OUR grounder's recall loss on quotes the model got right
  grounded            -> assertions that survived to be scored
A low F1 with high `quote_ungroundable` is a grounder problem, not a model problem.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv() -> None:
    """Read KEY=value lines from a gitignored .env. Never overrides a real env var."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv()

from rdcd.corpus import ncbi                                    # noqa: E402
from rdcd.corpus.jats import parse_jats                         # noqa: E402
from rdcd.eval.evalset import build_eval_papers                 # noqa: E402
from rdcd.eval.metrics import aggregate, score_case             # noqa: E402
from rdcd.extract.llm import (                                  # noqa: E402
    SYSTEM_PROMPT, LLMConfig, LLMExtractor, collect_batch, submit_batch,
)
from rdcd.ontology.store import STORE                           # noqa: E402
from rdcd.qa.provenance import ProvenanceVerifier               # noqa: E402

# claude-opus-5 list price, USD per token.
PRICE_IN, PRICE_OUT = 5.00 / 1e6, 25.00 / 1e6
CHARS_PER_TOKEN = 3.2      # conservative for dense scientific prose
EST_OUT_TOKENS = 3000      # generous per paper: multi-individual structured output


def dev_papers(track: str, split: str, limit: int | None):
    out = []
    for p in build_eval_papers():
        if p.track != track or p.split != split:
            continue
        if not p.pmcid or not ncbi.cached("pmcxml", f"pmcxml:{p.pmcid}", "xml"):
            continue
        doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
        if not doc.has_body:
            continue
        out.append((p, doc))
        if limit and len(out) >= limit:
            break
    return out


def estimate(items, cfg: LLMConfig) -> dict:
    chars = [min(len(d.text), cfg.max_chars) for _, d in items]
    n = len(items)
    doc_tok = sum(chars) / CHARS_PER_TOKEN
    sys_tok = len(SYSTEM_PROMPT) / CHARS_PER_TOKEN
    out_tok = n * EST_OUT_TOKENS
    # Caching: identical system prompt every call -> one write, n-1 reads.
    sys_cost = (sys_tok * PRICE_IN * 1.25) + (sys_tok * PRICE_IN * 0.10 * max(0, n - 1))
    sync = doc_tok * PRICE_IN + sys_cost + out_tok * PRICE_OUT
    return {
        "papers": n,
        "doc_chars_total": sum(chars),
        "doc_chars_mean": round(st.mean(chars)) if chars else 0,
        "est_input_tokens": round(doc_tok),
        "est_output_tokens": out_tok,
        "est_cost_sync_usd": round(sync, 2),
        "est_cost_batch_usd": round(sync * 0.5, 2),
        "system_prompt_tokens": round(sys_tok),
        "caching_saves_usd": round(sys_tok * PRICE_IN * n - sys_cost, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", default="single")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--effort", default="high")
    ap.add_argument("--dry-run", action="store_true", help="Estimate only; no API calls")
    ap.add_argument("--batch", action="store_true", help="Use the Batch API (50% cheaper)")
    ap.add_argument("--name", default="llm_dev")
    args = ap.parse_args()

    cfg = LLMConfig(effort=args.effort)
    items = dev_papers(args.track, args.split, args.limit)
    if not items:
        raise SystemExit(f"No cached papers for track={args.track} split={args.split}. "
                         "Run: make fetch-fulltext")

    est = estimate(items, cfg)
    print(f"track={args.track} split={args.split} effort={args.effort}")
    for k, v in est.items():
        print(f"  {k}: {v}")

    if args.dry_run:
        print("\ndry run: no API calls made.")
        print("To run for real:  make llm-dev        (synchronous)")
        print("                  make llm-dev-batch  (Batch API, ~50% cheaper)")
        return

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit(
            "No credentials. Put ANTHROPIC_API_KEY=sk-ant-... in a gitignored "
            f"{ROOT}/.env, or export it, then re-run."
        )

    ex = LLMExtractor(STORE, cfg)
    pv = ProvenanceVerifier(STORE)
    stats, scores, verdicts, usage = Counter(), [], [], Counter()
    t0 = time.time()

    if args.batch:
        print(f"\nsubmitting {len(items)} papers as one batch ...")
        job = submit_batch(((p.pmid, doc, p.source_doc()) for p, doc in items), cfg=cfg)
        print(f"  batch id: {job.batch_id}")
        print("  batches complete within ~1h typically, 24h max. Poll with:")
        print("    python3 -c \"import anthropic;"
              f"print(anthropic.Anthropic().messages.batches.retrieve('{job.batch_id}')"
              ".processing_status)\"")
        print("  then collect and score with rdcd.extract.llm.collect_batch().")
        (ROOT / "reports" / f"{args.name}_batch.json").write_text(
            json.dumps({"batch_id": job.batch_id, "custom_ids": job.custom_ids}, indent=1))
        return

    for i, (p, doc) in enumerate(items, 1):
        src = p.source_doc()
        try:
            recs = ex.extract(doc, src)
        except Exception as e:  # noqa: BLE001 - one bad paper must not kill the run
            print(f"  ! {p.pmid}: {type(e).__name__}: {e}")
            stats["api_error"] += 1
            continue
        if ex.last_stats:
            for k, v in ex.last_stats.to_dict().items():
                stats[k] += v
        if ex.last_usage:
            for k, v in ex.last_usage.items():
                if isinstance(v, int):
                    usage[k] += v
        if not recs:
            stats["no_records"] += 1
            continue
        # Track SINGLE gold is one individual; take the model's first individual.
        pred, _ = recs[0].enforce_provenance()
        verdicts.extend(pv.verify(doc, pred))
        scores.append(score_case(STORE, pred, p.gold_cases[0]))
        if i % 10 == 0:
            print(f"  {i}/{len(items)} papers, {time.time()-t0:.0f}s", flush=True)

    report = {
        "extractor": ex.name,
        "track": args.track, "split": args.split, "effort": args.effort,
        "estimate": est,
        "grounding": dict(stats),
        "usage": dict(usage),
        "actual_cost_usd": round(
            usage["input_tokens"] * PRICE_IN + usage["output_tokens"] * PRICE_OUT, 2),
        "provenance": pv.rates(verdicts),
        "metrics": aggregate(scores),
        "elapsed_s": round(time.time() - t0),
    }
    out = ROOT / "reports" / f"eval_{args.name}.json"
    out.write_text(json.dumps(report, indent=1))

    g = report["grounding"]
    returned = g.get("findings_returned", 0) or 1
    print(f"\n=== grounding (read this BEFORE the F1) ===")
    print(f"  findings returned by model:   {g.get('findings_returned', 0)}")
    print(f"  quote not found (halluc.):    {g.get('quote_not_found', 0)}"
          f"  ({g.get('quote_not_found', 0)/returned:.1%})")
    print(f"  quote ungroundable (our gap): {g.get('quote_ungroundable', 0)}"
          f"  ({g.get('quote_ungroundable', 0)/returned:.1%})")
    print(f"  grounded and scored:          {g.get('grounded', 0)}")
    print(f"\n=== provenance ===  support rate {report['provenance']['support_rate']}")
    m = report["metrics"]
    print(f"\n=== metrics (n={m['n_cases']}) vs dictionary baseline 0.5597 / target 0.85 ===")
    print(f"  observed graded F1: {m['observed_graded']['micro']['f1']:.4f}"
          f"  CI {m['observed_graded']['f1_ci95']}")
    print(f"  observed exact  F1: {m['observed_exact']['micro']['f1']:.4f}")
    print(f"  absent          F1: {m['excluded_exact']['micro']['f1']:.4f}   (baseline 0.1186)")
    print(f"  gene            F1: {m['gene_exact']['micro']['f1']:.4f}   (baseline 0.8872)")
    print(f"  diagnosis       F1: {m['disease_normalised']['micro']['f1']:.4f}   (baseline 0.2451)")
    print(f"\nactual cost: ${report['actual_cost_usd']}  (estimated ${est['est_cost_sync_usd']})")
    print(f"cache reads: {usage.get('cache_read_input_tokens', 0):,} tokens")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
