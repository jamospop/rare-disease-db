"""Run an extractor over the eval set and produce a scored, reproducible report.

Offline by default: it scores only papers whose full text is already cached, so
`make eval` cannot silently become a network benchmark whose numbers depend on
what NCBI served that afternoon. Fetching is a separate, explicit step.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..corpus import ncbi
from ..corpus.jats import parse_jats
from ..ontology.store import OntologyStore
from ..schema import CaseRecord
from .evalset import TRACK_PAPER, TRACK_SINGLE, EvalPaper, build_eval_papers
from .metrics import CaseScore, aggregate, score_case

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports"


def _git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "uncommitted"
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass
class RunResult:
    extractor: str
    scores_by_group: dict[str, list[CaseScore]] = field(default_factory=dict)
    n_papers_scored: int = 0
    n_papers_skipped_no_cache: int = 0
    n_papers_no_body: int = 0
    dropped_unprovenanced: int = 0

    def report(self, *, with_ci: bool = True) -> dict:
        return {
            "extractor": self.extractor,
            "environment": {
                "git_rev": _git_rev(),
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "coverage": {
                "papers_scored": self.n_papers_scored,
                "papers_skipped_no_cache": self.n_papers_skipped_no_cache,
                "papers_without_body": self.n_papers_no_body,
                "assertions_dropped_unprovenanced": self.dropped_unprovenanced,
            },
            "groups": {
                g: aggregate(s, with_ci=with_ci) for g, s in sorted(self.scores_by_group.items())
            },
        }


def run_extractor(
    store: OntologyStore,
    extractor,
    *,
    papers: Sequence[EvalPaper] | None = None,
    splits: tuple[str, ...] = ("dev", "test"),
    cache_only: bool = True,
    enforce_provenance: bool = True,
    limit: int | None = None,
    progress: bool = True,
) -> RunResult:
    papers = list(papers if papers is not None else build_eval_papers())
    papers = [p for p in papers if p.split in splits]
    if limit:
        papers = papers[:limit]
    res = RunResult(extractor=getattr(extractor, "name", str(extractor)))
    groups: dict[str, list[CaseScore]] = {}

    for i, p in enumerate(papers, 1):
        if not p.pmcid:
            res.n_papers_skipped_no_cache += 1
            continue
        if cache_only and not ncbi.cached("pmcxml", f"pmcxml:{p.pmcid}", "xml"):
            res.n_papers_skipped_no_cache += 1
            continue
        try:
            doc = parse_jats(ncbi.pmc_fulltext_xml(p.pmcid))
        except Exception:  # noqa: BLE001
            res.n_papers_skipped_no_cache += 1
            continue
        if not doc.has_body:
            res.n_papers_no_body += 1
            continue

        preds = extractor.extract(doc, p.source_doc())
        if not preds:
            continue
        pred = preds[0]
        if enforce_provenance:
            pred, dropped = pred.enforce_provenance()
            res.dropped_unprovenanced += len(dropped)

        gold = p.gold_cases[0] if p.track == TRACK_SINGLE else p.union_gold
        cs = score_case(store, pred, gold)
        res.n_papers_scored += 1
        for key in (
            f"{p.track}/{p.split}",
            f"{p.track}/all",
            f"all/{p.split}",
            "all/all",
        ):
            groups.setdefault(key, []).append(cs)
        if progress and i % 100 == 0:
            print(f"  scored {res.n_papers_scored}/{i}")

    res.scores_by_group = groups
    return res


def write_report(res: RunResult, name: str | None = None) -> Path:
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"eval_{name or res.extractor}.json"
    out.write_text(json.dumps(res.report(), indent=1))
    return out


def format_summary(report: dict) -> str:
    """A compact table. The primary metric is named, never implied."""
    lines = [
        f"extractor: {report['extractor']}   git: {report['environment']['git_rev']}",
        f"coverage: {report['coverage']['papers_scored']} papers scored, "
        f"{report['coverage']['papers_skipped_no_cache']} skipped (no cached text), "
        f"{report['coverage']['papers_without_body']} without body",
        "",
        f"{'group':16} {'n':>5}  {'obs exact F1':>12} {'obs graded F1':>13} "
        f"{'graded 95% CI':>18} {'excl F1':>8} {'gene F1':>8} {'dx F1':>7}",
    ]
    for g, m in report["groups"].items():
        ci = m["observed_graded"]["f1_ci95"]
        lines.append(
            f"{g:16} {m['n_cases']:>5}  "
            f"{m['observed_exact']['micro']['f1']:>12.4f} "
            f"{m['observed_graded']['micro']['f1']:>13.4f} "
            f"{'[%.3f, %.3f]' % (ci[0], ci[1]):>18} "
            f"{m['excluded_exact']['micro']['f1']:>8.4f} "
            f"{m['gene_exact']['micro']['f1']:>8.4f} "
            f"{m['disease_normalised']['micro']['f1']:>7.4f}"
        )
    return "\n".join(lines)
