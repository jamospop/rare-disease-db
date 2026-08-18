"""Find published cases that resemble a given set of phenotypes.

This is the part a person can use. A clinician or a family facing an undiagnosed
condition does not want a corpus; they want the handful of published patients who
look like this one, and what those turned out to be.

The ranker in `rdcd.eval.diagnose` answers "which disease?" from HPO's curated
disease profiles. This answers a different and complementary question: "which
published *patients* look like mine?" - with a citation for each, so the answer can
be read, checked, and taken to a clinician rather than believed.

Both are scored the same way (symmetric best-match information content over the HPO
DAG), so a rare, specific shared finding counts for far more than a common one.
Sharing "seizure" with a case means little; sharing "reduced cerebral white matter
volume" means a great deal.

Deliberately not a diagnostic device. It ranks published literature by phenotype
overlap. See the caveats attached to every response.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..ontology.store import OntologyStore


@dataclass
class CaseHit:
    case_id: str
    score: float
    shared: list[tuple[str, str, float]] = field(default_factory=list)  # id, label, IC
    n_case_phenotypes: int = 0
    diagnoses: list[dict] = field(default_factory=list)
    genes: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)
    qa_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "score": round(self.score, 4),
            "shared_phenotypes": [
                {"id": i, "label": l, "information_content": round(ic, 2)}
                for i, l, ic in self.shared
            ],
            "n_shared": len(self.shared),
            "n_case_phenotypes": self.n_case_phenotypes,
            "diagnoses": self.diagnoses,
            "genes": self.genes,
            "source": self.source,
            "qa_flags": self.qa_flags,
        }


class CaseSimilarity:
    """Rank cases by phenotype similarity to a query.

    Cases are supplied as (case_id, observed HPO ids, payload) so this works over a
    release file, a database, or anything else without knowing the storage.
    """

    def __init__(self, store: OntologyStore):
        self.store = store
        self._pair: dict[tuple[str, str], float] = {}
        self.cases: list[tuple[str, frozenset[str], dict]] = []

    def add(self, case_id: str, hpo_ids: Iterable[str], payload: dict) -> None:
        terms = frozenset(t for t in (self.store.hpo.normalize(h) for h in hpo_ids) if t)
        if terms:
            self.cases.append((case_id, terms, payload))

    def pair_ic(self, a: str, b: str) -> float:
        key = (a, b) if a <= b else (b, a)
        hit = self._pair.get(key)
        if hit is None:
            shared = self.store.hpo.ancestors(a) & self.store.hpo.ancestors(b)
            hit = max((self.store.information_content(x) for x in shared), default=0.0)
            self._pair[key] = hit
        return hit

    def search(
        self, query: Sequence[str], *, top: int = 20, min_shared_ic: float = 1.5
    ) -> list[CaseHit]:
        q = [t for t in (self.store.hpo.normalize(x) for x in query) if t]
        if not q:
            return []
        q_set = set(q)
        hits: list[CaseHit] = []
        for case_id, terms, payload in self.cases:
            # Symmetric best-match: each query term's best match in the case, and
            # each case term's best match in the query. Asymmetric scoring would let
            # a case with 80 phenotypes match everything.
            fwd = sum(max((self.pair_ic(x, t) for t in terms), default=0.0) for x in q)
            rev = sum(max((self.pair_ic(t, x) for x in q), default=0.0) for t in terms)
            score = (fwd / len(q) + rev / len(terms)) / 2
            if score <= 0:
                continue
            # Explain the match: which findings are shared, and how informative each
            # is. A hit nobody can interrogate is not usable evidence.
            shared = []
            for t in terms:
                best_id, best_ic = None, 0.0
                for x in q_set:
                    ic = self.pair_ic(t, x)
                    if ic > best_ic:
                        best_id, best_ic = t, ic
                if best_id and best_ic >= min_shared_ic:
                    shared.append((best_id, self.store.hpo.label(best_id) or best_id, best_ic))
            shared.sort(key=lambda r: -r[2])
            hits.append(CaseHit(
                case_id=case_id, score=score, shared=shared[:12],
                n_case_phenotypes=len(terms),
                diagnoses=payload.get("diagnoses", []),
                genes=payload.get("genes", []),
                source=payload.get("source", {}),
                qa_flags=payload.get("qa_flags", []),
            ))
        hits.sort(key=lambda h: (-h.score, h.case_id))
        return hits[:top]

    def disease_tally(self, hits: Sequence[CaseHit]) -> list[dict]:
        """What did the similar cases turn out to be, weighted by similarity.

        The question behind the question: not "which case is closest" but "what do
        cases like this one usually turn out to be".
        """
        agg: dict[str, dict] = {}
        for h in hits:
            for d in h.diagnoses:
                key = d.get("id")
                if not key:
                    continue
                e = agg.setdefault(key, {"id": key, "label": d.get("label"),
                                         "n_cases": 0, "score": 0.0, "case_ids": []})
                e["n_cases"] += 1
                e["score"] += h.score
                e["case_ids"].append(h.case_id)
        out = sorted(agg.values(), key=lambda e: -e["score"])
        for e in out:
            e["score"] = round(e["score"], 3)
        return out
