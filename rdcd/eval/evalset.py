"""Construction of the evaluation set, with an explicit unit of comparison.

The gold set is mostly multi-individual cohort papers (median 2 cases per paper,
max 462). That makes "case-level F1" ambiguous: to compare per individual you
must first solve individual segmentation, and a segmentation error would be
charged to the phenotype extractor. So we define two tracks and always report
which one a number came from.

Track SINGLE  - papers contributing exactly one gold case. Prediction and gold
                are directly comparable, no segmentation involved. This is the
                clean measure of extraction quality.
Track PAPER   - all papers. Gold is the union of every individual's features in
                that paper, prediction is everything found in the document.
                Segmentation-free, measures corpus-scale recall, and is
                systematically kinder on recall and harsher on precision.

Dev/test split is by a hash of the PMID, so it is stable as the gold set grows
and no paper can migrate between splits between runs. All calibration happens on
dev; test is scored once per reported release.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..schema import CaseRecord, SourceDoc
from .goldsets import Availability, group_by_pmid, load_gold

DATA = Path(__file__).resolve().parents[2] / "data"
AVAIL_REPORT = Path(__file__).resolve().parents[2] / "reports" / "goldset_availability.json"

TRACK_SINGLE = "single"
TRACK_PAPER = "paper"


def split_of(pmid: str, *, dev_share: float = 0.5) -> str:
    """Deterministic dev/test assignment from the PMID alone."""
    h = int(hashlib.sha256(f"rdcd:{pmid}".encode()).hexdigest()[:8], 16)
    return "dev" if (h % 10_000) / 10_000 < dev_share else "test"


@dataclass
class EvalPaper:
    pmid: str
    pmcid: str | None
    tier: str
    license: str | None
    quotes_permitted: bool
    split: str
    gold_cases: list[CaseRecord] = field(default_factory=list)

    @property
    def track(self) -> str:
        return TRACK_SINGLE if len(self.gold_cases) == 1 else TRACK_PAPER

    @property
    def union_gold(self) -> CaseRecord:
        """One synthetic record holding every assertion in the paper (Track PAPER)."""
        base = self.gold_cases[0]
        phenos, diags, variants = [], [], []
        seen_p, seen_d, seen_v = set(), set(), set()
        for c in self.gold_cases:
            for p in c.phenotypes:
                k = (p.term.id, p.excluded)
                if k not in seen_p:
                    seen_p.add(k)
                    phenos.append(p)
            for d in c.diagnoses:
                if d.disease.id not in seen_d:
                    seen_d.add(d.disease.id)
                    diags.append(d)
            for v in c.variants:
                k = (v.gene.id if v.gene else None, v.hgvs_c)
                if k not in seen_v:
                    seen_v.add(k)
                    variants.append(v)
        return base.model_copy(
            update={
                "id": f"PMID_{self.pmid}_union",
                "phenotypes": phenos,
                "diagnoses": diags,
                "variants": variants,
            }
        )

    def source_doc(self) -> SourceDoc:
        return SourceDoc(
            pmid=self.pmid,
            pmcid=self.pmcid,
            license=self.license,
            in_oa_subset=self.tier.startswith("full_text"),
            quotes_permitted=self.quotes_permitted,
        )


def load_availability() -> dict[str, dict]:
    if not AVAIL_REPORT.exists():
        raise FileNotFoundError(f"{AVAIL_REPORT} missing. Run: make audit")
    return json.loads(AVAIL_REPORT.read_text())["per_paper"]


def build_eval_papers(
    *, tiers: tuple[str, ...] = ("full_text_quotable", "full_text_facts_only"),
    include_retracted: bool = False,
) -> list[EvalPaper]:
    """Every gold paper whose source text we are allowed to read."""
    avail = load_availability()
    by_pmid = group_by_pmid(load_gold())
    out: list[EvalPaper] = []
    for pmid, cases in sorted(by_pmid.items()):
        a = avail.get(pmid)
        if not a or a["tier"] not in tiers:
            continue
        if a.get("retracted") and not include_retracted:
            continue
        out.append(
            EvalPaper(
                pmid=pmid,
                pmcid=a.get("pmcid"),
                tier=a["tier"],
                license=a.get("license"),
                quotes_permitted=bool(a.get("quotes_permitted")),
                split=split_of(pmid),
                gold_cases=cases,
            )
        )
    return out


def summarise(papers: list[EvalPaper]) -> dict:
    def count(pred) -> dict:
        sel = [p for p in papers if pred(p)]
        return {"papers": len(sel), "gold_cases": sum(len(p.gold_cases) for p in sel)}

    return {
        "total": count(lambda p: True),
        "by_split": {s: count(lambda p, s=s: p.split == s) for s in ("dev", "test")},
        "by_track": {t: count(lambda p, t=t: p.track == t) for t in (TRACK_SINGLE, TRACK_PAPER)},
        "by_tier": {
            t: count(lambda p, t=t: p.tier == t)
            for t in ("full_text_quotable", "full_text_facts_only")
        },
        "single_track_by_split": {
            s: count(lambda p, s=s: p.track == TRACK_SINGLE and p.split == s)
            for s in ("dev", "test")
        },
    }
