"""Scoring behaviour, including the properties that make the numbers honest."""
from __future__ import annotations

import pytest

from rdcd.eval.metrics import (
    GRADED_SIM_THRESHOLD, PRF, bootstrap_ci, exact_prf, graded_prf,
    jaccard_closure, lin_similarity, mean_reciprocal_rank, topk_recall,
)
from rdcd.ontology.store import STORE


@pytest.fixture(scope="module")
def store():
    return STORE


SEIZURE, FOCAL, MICROCEPHALY = "HP:0001250", "HP:0007359", "HP:0000252"


def test_exact_prf_counts():
    prf = exact_prf(["a", "b"], ["b", "c"])
    assert (prf.tp, prf.fp, prf.fn) == (1, 1, 1)
    assert prf.f1 == pytest.approx(0.5)


def test_lin_similarity_bounds(store):
    assert lin_similarity(store, SEIZURE, SEIZURE) == 1.0
    assert 0 < lin_similarity(store, SEIZURE, FOCAL) < 1


def test_graded_scoring_credits_a_near_miss_more_than_exact_does(store):
    gold = [SEIZURE, MICROCEPHALY]
    pred = [FOCAL, MICROCEPHALY]      # one exact hit, one parent/child near miss
    assert graded_prf(store, pred, gold).f1 > exact_prf(pred, gold).f1


def test_threshold_removes_the_free_credit_floor(store):
    """Unrelated HPO terms still share high-level ancestors.

    Without a threshold, a wholly wrong prediction scores ~0.26 and a headline
    F1 could be met by coincidence. This pins the floor at zero.
    """
    unrelated = graded_prf(store, ["HP:0001873", "HP:0002315"], [SEIZURE, MICROCEPHALY])
    assert unrelated.f1 == 0.0
    assert lin_similarity(store, SEIZURE, MICROCEPHALY) < GRADED_SIM_THRESHOLD


def test_graded_precision_recall_survive_summation(store):
    """Micro-averaging adds PRFs, so graded numerators must add correctly too."""
    a = graded_prf(store, [FOCAL], [SEIZURE])
    b = graded_prf(store, [MICROCEPHALY], [MICROCEPHALY])
    total = a + b
    assert total.precision == pytest.approx((a.tp_p + b.tp_p) / 2)
    assert 0 < total.f1 <= 1.0


def test_empty_prediction_is_all_false_negatives(store):
    prf = graded_prf(store, [], [SEIZURE, MICROCEPHALY])
    assert (prf.tp, prf.fn) == (0.0, 2.0)
    assert prf.recall == 0.0


def test_jaccard_closure_is_one_for_identical_profiles(store):
    assert jaccard_closure(store, [SEIZURE], [SEIZURE]) == 1.0


def test_bootstrap_ci_is_deterministic_and_brackets_the_estimate():
    from rdcd.eval.metrics import CaseScore

    scores = [CaseScore(case_id=str(i), observed_exact=PRF(tp=3, fp=1, fn=1)) for i in range(40)]
    lo, hi = bootstrap_ci(scores, "observed_exact", "f1", n=200)
    again = bootstrap_ci(scores, "observed_exact", "f1", n=200)
    assert (lo, hi) == again          # fixed seed -> reproducible interval
    assert lo <= 0.75 <= hi


def test_topk_and_mrr():
    ranked = ["MONDO:1", "MONDO:2", "MONDO:3"]
    assert topk_recall(ranked, ["MONDO:3"], (1, 3)) == {1: 0, 3: 1}
    assert mean_reciprocal_rank(ranked, ["MONDO:3"]) == pytest.approx(1 / 3)
