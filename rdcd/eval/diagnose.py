"""A phenotype-driven diagnostic ranker, and the measurement that matters.

The project's claim is that a structured case corpus improves rare-disease
diagnostic support. That claim is only testable if there is a ranker to plug
phenotypes into, so here is a transparent one: score each candidate disease by
the information content its curated HPO profile shares with the query
phenotypes. This is the classic Phenomizer-style approach, deliberately simple
and fully reproducible.

The measurement design is the point:

  CEILING  - rank using the expert gold phenotypes. How well can this ranker do
             when extraction is perfect? Anything above this is unreachable by
             improving extraction.
  PIPELINE - rank using our extracted phenotypes. The real end-to-end number.

CEILING minus PIPELINE is exactly what extraction error costs diagnosis, in
top-k recall. That gap, not raw F1, is what tells you whether to spend the next
month on extraction or on the ranker.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

from ..ontology.store import OntologyStore


@dataclass(slots=True)
class Ranked:
    disease_id: str
    score: float
    label: str | None = None


class PhenotypeRanker:
    """Symmetric best-match information-content scoring over HPO profiles."""

    def __init__(self, store: OntologyStore, *, min_profile: int = 2):
        self.store = store
        self.min_profile = min_profile
        self._pair_cache: dict[tuple[str, str], float] = {}

    @property
    def profiles(self) -> dict[str, set[str]]:
        return self._profiles

    @property
    def _profiles(self) -> dict[str, set[str]]:
        if not hasattr(self, "__profiles"):
            self.__profiles = {
                d: p for d, p in self.store.disease_profiles.items()
                if len(p) >= self.min_profile
            }
        return self.__profiles

    @property
    def annotated_terms(self) -> set[str]:
        if not hasattr(self, "__annterms"):
            t: set[str] = set()
            for p in self._profiles.values():
                t |= p
            self.__annterms = t
        return self.__annterms

    def pair_ic(self, a: str, b: str) -> float:
        """IC of the most informative common ancestor. Memoised and symmetric.

        This is the whole cost of the ranker. Memoising it across diseases *and*
        across queries is what makes a corpus-wide benchmark finish in minutes
        rather than hours: distinct (term, term) pairs are far fewer than
        (query, disease, term) triples.
        """
        key = (a, b) if a <= b else (b, a)
        hit = self._pair_cache.get(key)
        if hit is not None:
            return hit
        shared = self.store.hpo.ancestors(a) & self.store.hpo.ancestors(b)
        val = max((self.store.information_content(x) for x in shared), default=0.0)
        self._pair_cache[key] = val
        return val

    def _best_match_ic(self, query: Sequence[str], profile: set[str]) -> float:
        """Sum over query terms of the IC of their best match in the profile."""
        return sum(max((self.pair_ic(q, t) for t in profile), default=0.0) for q in query)

    def rank(self, phenotypes: Iterable[str], *, top: int = 20,
             candidates: Iterable[str] | None = None) -> list[Ranked]:
        query = [t for t in (self.store.hpo.normalize(p) for p in phenotypes) if t]
        if not query:
            return []
        # Only diseases sharing at least one annotated term with the query can
        # score above zero, so restrict the candidate set to those. This is an
        # exact optimisation, not an approximation.
        prof = self.profiles
        if candidates is not None:
            prof = {d: p for d, p in prof.items() if d in set(candidates)}
        # Precompute best-match IC from every annotated term to the query once,
        # then scoring each disease is pure lookups.
        best_to_query: dict[str, float] = {
            t: max((self.pair_ic(q, t) for q in query), default=0.0)
            for t in self.annotated_terms
        }
        scored: list[Ranked] = []
        for d, p in prof.items():
            rev = sum(best_to_query.get(t, 0.0) for t in p)
            if rev <= 0.0:
                continue
            fwd = sum(max((self.pair_ic(q, t) for t in p), default=0.0) for q in query)
            # Length-normalised symmetric score: without normalising, diseases
            # with huge annotation profiles win regardless of relevance.
            score = (fwd / len(query) + rev / len(p)) / 2
            scored.append(Ranked(d, round(score, 5), self.store.mondo.label(d)))
        scored.sort(key=lambda r: (-r.score, r.disease_id))
        return scored[:top]
