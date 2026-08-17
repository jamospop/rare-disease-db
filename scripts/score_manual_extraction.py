#!/usr/bin/env python3
"""Ground and score extraction JSON produced without API access.

Uses the same ResponseParser as the API path, so quote verification, HPO grounding,
provenance construction, and scoring are identical. The only difference is transport.

Reports grounding statistics before any F1, for the same reason the API runner does:
a low F1 with a high `quote_ungroundable` count is a grounder problem, not an
extractor problem, and conflating the two would misdirect the next month of work.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.corpus import ncbi
from rdcd.corpus.jats import parse_jats
from rdcd.eval.evalset import build_eval_papers
from rdcd.eval.metrics import aggregate, score_case
from rdcd.extract.llm import EXTRACTION_TOOL, ResponseParser
from rdcd.ontology.store import STORE
from rdcd.qa import constraints
from rdcd.qa.provenance import ProvenanceVerifier

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "manual_extraction"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="manual")
    ap.add_argument("--extractor", default="claude-code-insession-v1")
    args = ap.parse_args()

    manifest = json.loads((WORK / "_MANIFEST.json").read_text())
    wanted = {p["pmid"] for p in manifest["papers"]}
    by_pmid = {p.pmid: p for p in build_eval_papers() if p.pmid in wanted}

    parser = ResponseParser(STORE)
    pv = ProvenanceVerifier(STORE)
    stats, verdicts, viol = Counter(), [], Counter()
    # Track SINGLE gold has one individual, but a cohort paper legitimately yields
    # several. Taking the FIRST predicted individual is arbitrary: if the curator
    # picked patient 17 and the paper lists patient 5 first, phenotypes may match
    # while the gene is charged as wrong for a reason that is not an extraction
    # error. So report both, and never quote only the flattering one:
    #   first      - predicted individual [0], the strict reading
    #   best_match - the predicted individual with the highest graded phenotype F1,
    #                i.e. set-vs-set assignment rather than positional
    # The dictionary baseline emits exactly one record per document, so its numbers
    # are identical under both and the comparison stays fair.
    scores_first, scores_best = [], []
    per_paper, missing = [], []

    for pmid in sorted(wanted):
        jf = WORK / f"{pmid}.json"
        if not jf.exists():
            missing.append(pmid)
            continue
        p = by_pmid[pmid]
        doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
        args_obj = json.loads(jf.read_text())
        # Accept either the bare tool arguments or a full response envelope.
        if "content" in args_obj:
            args_obj = parser.tool_input(args_obj)
        recs, st = parser.parse(doc, p.source_doc(), args_obj, extractor=args.extractor)
        for k, v in st.to_dict().items():
            stats[k] += v
        if not recs:
            stats["no_records"] += 1
            continue
        cleaned = [r.enforce_provenance()[0] for r in recs]
        gold = p.gold_cases[0]
        cand = [score_case(STORE, r, gold) for r in cleaned]
        cs_first = cand[0]
        best_i = max(range(len(cand)), key=lambda i: cand[i].observed_graded.f1)
        cs_best = cand[best_i]
        scores_first.append(cs_first)
        scores_best.append(cs_best)
        # QA is reported on the individual actually selected by best-match.
        verdicts.extend(pv.verify(doc, cleaned[best_i]))
        for v in constraints.check(STORE, cleaned[best_i]):
            viol[f"{v.severity}:{v.code}"] += 1
        per_paper.append({
            "pmid": pmid,
            "n_individuals_returned": len(recs),
            "best_match_index": best_i,
            "pred_observed": len(cleaned[best_i].observed_hpo),
            "gold_observed": cs_best.n_gold_observed,
            "pred_excluded": len(cleaned[best_i].excluded_hpo),
            "observed_graded_f1_first": round(cs_first.observed_graded.f1, 4),
            "observed_graded_f1_best": round(cs_best.observed_graded.f1, 4),
            "gene_f1_best": round(cs_best.gene_exact.f1, 4),
        })

    report = {
        "extractor": args.extractor,
        "transport": "no API; extraction produced by an assistant with filesystem access",
        "track": manifest["track"], "split": manifest["split"],
        "papers_prepared": len(wanted), "papers_extracted": len(scores_best),
        "papers_missing_json": missing,
        "grounding": dict(stats),
        "provenance": pv.rates(verdicts),
        "constraint_violations": dict(viol.most_common()),
        "metrics": aggregate(scores_best),
        "metrics_first_individual": aggregate(scores_first),
        "multi_individual_papers": sum(1 for r in per_paper if r["n_individuals_returned"] > 1),
        "per_paper": per_paper,
    }
    out = ROOT / "reports" / f"eval_{args.name}.json"
    out.write_text(json.dumps(report, indent=1))

    g = report["grounding"]; ret = g.get("findings_returned", 0) or 1
    print(f"extractor: {args.extractor}   papers scored: {len(scores_best)}/{len(wanted)}")
    if missing:
        print(f"  missing JSON for {len(missing)} papers: {missing[:8]}")
    print(f"\n=== grounding (read BEFORE the F1) ===")
    print(f"  findings returned:            {g.get('findings_returned', 0)}")
    print(f"  quote not found (halluc.):    {g.get('quote_not_found', 0)} ({g.get('quote_not_found',0)/ret:.1%})")
    print(f"  quote ungroundable (our gap): {g.get('quote_ungroundable', 0)} ({g.get('quote_ungroundable',0)/ret:.1%})")
    print(f"  grounded and scored:          {g.get('grounded', 0)}")
    print(f"  individuals returned:         {g.get('individuals', 0)}")
    print(f"\n=== provenance === support {report['provenance']['support_rate']}")
    print(f"=== constraints === {report['constraint_violations'] or 'none'}")
    m, mf = report["metrics"], report["metrics_first_individual"]
    b = {"graded": 0.5597, "exact": 0.4110, "absent": 0.1186, "gene": 0.8872, "dx": 0.2451}
    print(f"\n=== metrics (n={m['n_cases']}) ===")
    print(f"  {report['multi_individual_papers']} papers returned >1 individual, so "
          f"first-vs-best differ there only.")
    print(f"{'':22} {'best-match':>11} {'first indiv':>12} {'dict base':>10} {'delta*':>8}")
    for label, key in [
        ("observed graded F1", "observed_graded"),
        ("observed exact  F1", "observed_exact"),
        ("absent          F1", "excluded_exact"),
        ("gene            F1", "gene_exact"),
        ("diagnosis       F1", "disease_normalised"),
    ]:
        base = b[{"observed_graded": "graded", "observed_exact": "exact",
                  "excluded_exact": "absent", "gene_exact": "gene",
                  "disease_normalised": "dx"}[key]]
        v, vf = m[key]["micro"]["f1"], mf[key]["micro"]["f1"]
        print(f"  {label:20} {v:>11.4f} {vf:>12.4f} {base:>10.4f} {v-base:>+8.4f}")
    ci = m["observed_graded"]["f1_ci95"]
    cif = mf["observed_graded"]["f1_ci95"]
    print(f"\n  * delta is best-match vs the dictionary baseline on the same track/split.")
    print(f"  primary metric 95% CI: best-match [{ci[0]}, {ci[1]}]  "
          f"first-individual [{cif[0]}, {cif[1]}]   (plan target 0.85)")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
