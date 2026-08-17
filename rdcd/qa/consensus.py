"""Tri-model consensus: independent extractors vote, disagreement lowers trust.

Agreement between models that share a training distribution is weaker evidence
than it looks, so consensus here sets a *published confidence* and routes records
to a review queue. It is never used to manufacture certainty, and a unanimous
record is still only as good as its provenance check.

Confidence is the agreement fraction per assertion: an assertion found by all
three extractors scores 1.0, by one of three scores 0.33. Those numbers ship with
the data so a downstream user can filter on them.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..schema import CaseRecord, PhenotypeAssertion


@dataclass
class ConsensusResult:
    merged: CaseRecord
    n_extractors: int
    agreement: dict[str, float] = field(default_factory=dict)
    review_queue: list[str] = field(default_factory=list)

    @property
    def mean_agreement(self) -> float:
        return sum(self.agreement.values()) / len(self.agreement) if self.agreement else 0.0


def merge(records: list[CaseRecord], *, review_below: float = 0.67) -> ConsensusResult:
    """Union the assertions, weight each by how many extractors found it."""
    if not records:
        raise ValueError("no records to merge")
    n = len(records)
    base = records[0]

    votes: dict[tuple[str, bool], list[PhenotypeAssertion]] = defaultdict(list)
    for r in records:
        seen: set[tuple[str, bool]] = set()
        for p in r.phenotypes:
            key = (p.term.id, p.excluded)
            if key in seen:      # one vote per extractor per assertion
                continue
            seen.add(key)
            votes[key].append(p)

    agreement: dict[str, float] = {}
    review: list[str] = []
    merged_ph: list[PhenotypeAssertion] = []
    for (term, excluded), group in votes.items():
        frac = len(group) / n
        key = f"{term}{'!' if excluded else ''}"
        agreement[key] = round(frac, 4)
        ev = [e for g in group for e in g.evidence][:5]
        merged_ph.append(group[0].model_copy(update={"evidence": ev}))
        if frac < review_below:
            review.append(key)

    dis_votes: dict[str, int] = defaultdict(int)
    for r in records:
        for d in {x.disease.id for x in r.diagnoses}:
            dis_votes[d] += 1
    gene_votes: dict[str, int] = defaultdict(int)
    for r in records:
        for g in r.gene_ids:
            gene_votes[g] += 1
    for d, c in dis_votes.items():
        agreement[f"dx:{d}"] = round(c / n, 4)
        if c / n < review_below:
            review.append(f"dx:{d}")
    for g, c in gene_votes.items():
        agreement[f"gene:{g}"] = round(c / n, 4)
        if c / n < review_below:
            review.append(f"gene:{g}")

    mean = sum(agreement.values()) / len(agreement) if agreement else 0.0
    merged = base.model_copy(update={
        "phenotypes": merged_ph,
        "confidence": round(mean, 4),
        "extractors": sorted({e for r in records for e in r.extractors}),
        "qa_flags": sorted(set(base.qa_flags) | ({"consensus:review"} if review else set())),
    })
    return ConsensusResult(merged=merged, n_extractors=n, agreement=agreement,
                           review_queue=sorted(review))
