"""Scoring. Defined before any extractor exists, so the target cannot drift.

Three deliberate choices, each of which changes the headline number:

1. **Observed and excluded phenotypes are scored separately.** 59% of gold
   features are `excluded: true`. Pooling them lets a pipeline that ignores
   negation score well by flooding output with present-phenotypes, and lets one
   that inverts negation look catastrophic for the wrong reason.

2. **Ontology-aware credit as well as exact match.** Predicting HP:0007359
   "Focal seizure" where the curator wrote HP:0001250 "Seizure" is a good
   extraction, not a miss. We report exact F1 *and* graded F1 (Lin similarity
   over the HPO DAG, best-match on both sides), and never quote one as the other.

3. **Bootstrap confidence intervals on everything.** A headline F1 with no
   interval invites over-reading a difference that is noise. Case-level
   resampling, fixed seed, so the interval reproduces exactly.

The primary target metric referenced by the project plan (F1 >= 0.85) is
`PRIMARY_METRIC` below, so "the number" is unambiguous.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from ..ontology.store import OntologyStore
from ..schema import CaseRecord

PRIMARY_METRIC = "observed_phenotype_graded_f1"

# Unrelated HPO terms still share high-level ancestors, so Lin similarity has a
# non-zero floor (~0.26 for Seizure vs Microcephaly). Partial credit below this
# threshold is discarded, which pulls the random-prediction floor near zero and
# stops a headline F1 from being inflated by coincidence. Calibrated in
# scripts/calibrate_threshold.py; see docs/BENCHMARKS.md.
GRADED_SIM_THRESHOLD = 0.5
BOOTSTRAP_SEED = 20260817
BOOTSTRAP_N = 1000


# ---------------------------------------------------------------------------
# Set metrics
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PRF:
    """Counts, or graded mass when scoring is partial-credit.

    `tp_p`/`tp_r` let a graded scorer carry different numerators for precision
    and recall (a single general prediction can be fully correct as a prediction
    while covering only part of the gold set). For exact matching they are both
    None and `tp` is used for both, so the ordinary case stays simple.
    """

    tp: float = 0.0
    fp: float = 0.0
    fn: float = 0.0
    tp_p: float | None = None
    tp_r: float | None = None

    @property
    def precision(self) -> float:
        num = self.tp if self.tp_p is None else self.tp_p
        d = num + self.fp
        return num / d if d else 0.0

    @property
    def recall(self) -> float:
        num = self.tp if self.tp_r is None else self.tp_r
        d = num + self.fn
        return num / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def __add__(self, o: "PRF") -> "PRF":
        def _s(a: float | None, b: float | None, fa: float, fb: float) -> float | None:
            if a is None and b is None:
                return None
            return (a if a is not None else fa) + (b if b is not None else fb)

        return PRF(
            self.tp + o.tp, self.fp + o.fp, self.fn + o.fn,
            _s(self.tp_p, o.tp_p, self.tp, o.tp),
            _s(self.tp_r, o.tp_r, self.tp, o.tp),
        )

    def to_dict(self) -> dict:
        return {
            "tp": round(self.tp, 3), "fp": round(self.fp, 3), "fn": round(self.fn, 3),
            "precision": round(self.precision, 4), "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def exact_prf(pred: Iterable[str], gold: Iterable[str]) -> PRF:
    p, g = set(pred), set(gold)
    return PRF(tp=len(p & g), fp=len(p - g), fn=len(g - p))


# ---------------------------------------------------------------------------
# Ontology-aware similarity
# ---------------------------------------------------------------------------
def lin_similarity(store: OntologyStore, a: str, b: str) -> float:
    """Lin (1998) semantic similarity in [0,1] over the HPO DAG."""
    if a == b:
        return 1.0
    ha, hb = store.hpo.normalize(a), store.hpo.normalize(b)
    if not ha or not hb:
        return 0.0
    if ha == hb:
        return 1.0
    ica, icb = store.information_content(ha), store.information_content(hb)
    if ica + icb == 0:
        return 0.0
    _, ic_mica = store.most_informative_common_ancestor(ha, hb)
    return max(0.0, min(1.0, 2 * ic_mica / (ica + icb)))


def graded_prf(
    store: OntologyStore,
    pred: Iterable[str],
    gold: Iterable[str],
    *,
    sim: Callable[[OntologyStore, str, str], float] = lin_similarity,
    threshold: float = GRADED_SIM_THRESHOLD,
) -> PRF:
    """Best-match graded P/R.

    Each predicted term earns its best similarity against any gold term
    (precision side); each gold term earns its best similarity against any
    predicted term (recall side). Asymmetric on purpose: an extractor that emits
    one very general term should not get full recall on ten specific gold terms.
    """
    p, g = sorted(set(pred)), sorted(set(gold))
    if not p and not g:
        return PRF()
    if not p:
        return PRF(fn=float(len(g)))
    if not g:
        return PRF(fp=float(len(p)))
    prec_scores = []
    for x in p:
        best = max((sim(store, x, y) for y in g), default=0.0)
        prec_scores.append(best if best >= threshold else 0.0)
    rec_scores = []
    for y in g:
        best = max((sim(store, x, y) for x in p), default=0.0)
        rec_scores.append(best if best >= threshold else 0.0)
    tp_p, tp_r = sum(prec_scores), sum(rec_scores)
    # Encode graded precision and recall into a PRF whose .precision/.recall
    # reproduce them exactly: precision = tp_p/len(p), recall = tp_r/len(g).
    return PRF(tp=(tp_p + tp_r) / 2, fp=len(p) - tp_p, fn=len(g) - tp_r,
               tp_p=tp_p, tp_r=tp_r)


def jaccard_closure(store: OntologyStore, pred: Iterable[str], gold: Iterable[str]) -> float:
    """Jaccard over ancestor closures - a coarse whole-profile agreement measure."""
    pc: set[str] = set()
    for t in pred:
        pc |= store.hpo.ancestors(t)
    gc: set[str] = set()
    for t in gold:
        gc |= store.hpo.ancestors(t)
    if not pc and not gc:
        return 1.0
    return len(pc & gc) / len(pc | gc) if (pc | gc) else 0.0


# ---------------------------------------------------------------------------
# Per-case scoring
# ---------------------------------------------------------------------------
@dataclass
class CaseScore:
    case_id: str
    observed_exact: PRF = field(default_factory=PRF)
    observed_graded: PRF = field(default_factory=PRF)
    excluded_exact: PRF = field(default_factory=PRF)
    gene_exact: PRF = field(default_factory=PRF)
    disease_exact: PRF = field(default_factory=PRF)
    disease_normalised: PRF = field(default_factory=PRF)
    profile_jaccard: float = 0.0
    n_gold_observed: int = 0
    n_pred_observed: int = 0
    unprovenanced_pred: int = 0


def score_case(store: OntologyStore, pred: CaseRecord, gold: CaseRecord) -> CaseScore:
    obs_p = {t for t in (store.hpo.normalize(x) for x in pred.observed_hpo) if t}
    obs_g = {t for t in (store.hpo.normalize(x) for x in gold.observed_hpo) if t}
    exc_p = {t for t in (store.hpo.normalize(x) for x in pred.excluded_hpo) if t}
    exc_g = {t for t in (store.hpo.normalize(x) for x in gold.excluded_hpo) if t}

    gene_p = {s for s in (store.canonical_gene_symbol(x) for x in pred.gene_symbols) if s}
    gene_g = {s for s in (store.canonical_gene_symbol(x) for x in gold.gene_symbols) if s}

    dis_p_norm = {d for d in (store.normalize_disease(x) for x in pred.disease_ids) if d}
    dis_g_norm = {d for d in (store.normalize_disease(x) for x in gold.disease_ids) if d}

    return CaseScore(
        case_id=gold.id,
        observed_exact=exact_prf(obs_p, obs_g),
        observed_graded=graded_prf(store, obs_p, obs_g),
        excluded_exact=exact_prf(exc_p, exc_g),
        gene_exact=exact_prf(gene_p, gene_g),
        disease_exact=exact_prf(pred.disease_ids, gold.disease_ids),
        disease_normalised=exact_prf(dis_p_norm, dis_g_norm),
        profile_jaccard=jaccard_closure(store, obs_p, obs_g),
        n_gold_observed=len(obs_g),
        n_pred_observed=len(obs_p),
        unprovenanced_pred=len(pred.unprovenanced()),
    )


# ---------------------------------------------------------------------------
# Aggregation with bootstrap CIs
# ---------------------------------------------------------------------------
def _micro(scores: Sequence[CaseScore], attr: str) -> PRF:
    total = PRF()
    for s in scores:
        total = total + getattr(s, attr)
    return total


def bootstrap_ci(
    scores: Sequence[CaseScore],
    attr: str,
    stat: str = "f1",
    *,
    n: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI by resampling cases (the unit of independence)."""
    if not scores:
        return (0.0, 0.0)
    rng = random.Random(seed)
    k = len(scores)
    vals = []
    for _ in range(n):
        sample = [scores[rng.randrange(k)] for _ in range(k)]
        vals.append(getattr(_micro(sample, attr), stat))
    vals.sort()
    lo = vals[int((alpha / 2) * n)]
    hi = vals[min(n - 1, int((1 - alpha / 2) * n))]
    return (round(lo, 4), round(hi, 4))


def aggregate(scores: Sequence[CaseScore], *, with_ci: bool = True) -> dict:
    fields = [
        "observed_exact", "observed_graded", "excluded_exact",
        "gene_exact", "disease_exact", "disease_normalised",
    ]
    out: dict = {"n_cases": len(scores)}
    for f in fields:
        m = _micro(scores, f)
        entry = {"micro": m.to_dict()}
        per_case = [getattr(s, f).f1 for s in scores]
        entry["macro_f1"] = round(statistics.fmean(per_case), 4) if per_case else 0.0
        if with_ci:
            entry["f1_ci95"] = bootstrap_ci(scores, f, "f1")
        out[f] = entry
    out["profile_jaccard_mean"] = (
        round(statistics.fmean([s.profile_jaccard for s in scores]), 4) if scores else 0.0
    )
    out["unprovenanced_assertions"] = sum(s.unprovenanced_pred for s in scores)
    out["gold_observed_total"] = sum(s.n_gold_observed for s in scores)
    out["pred_observed_total"] = sum(s.n_pred_observed for s in scores)
    out["primary_metric"] = PRIMARY_METRIC
    out["primary_value"] = out["observed_graded"]["micro"]["f1"]
    return out


# ---------------------------------------------------------------------------
# Ranked-diagnosis metrics, for the benchmark campaign
# ---------------------------------------------------------------------------
def topk_recall(ranked: Sequence[str], gold: Iterable[str], ks: Sequence[int] = (1, 3, 5, 10, 20)) -> dict[int, int]:
    g = {x for x in gold if x}
    return {k: int(bool(g & set(ranked[:k]))) for k in ks}


def mean_reciprocal_rank(ranked: Sequence[str], gold: Iterable[str]) -> float:
    g = {x for x in gold if x}
    for i, r in enumerate(ranked, 1):
        if r in g:
            return 1.0 / i
    return 0.0
