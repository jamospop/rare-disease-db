"""QA: constraint checking, provenance verification, consensus."""
from __future__ import annotations

import pytest

from rdcd.ontology.store import STORE
from rdcd.qa import consensus, constraints
from rdcd.schema import (
    CaseRecord, DiagnosisAssertion, Evidence, OntologyClass,
    PhenotypeAssertion, SourceDoc, Subject, TimeElement, VariantAssertion,
)

EV = [Evidence(source_id="PMID:1", extractor="test")]


def rec(**kw) -> CaseRecord:
    base = dict(id="c", source=SourceDoc(pmid="1"))
    base.update(kw)
    return CaseRecord(**base)


def codes(record) -> set[str]:
    return {v.code for v in constraints.check(STORE, record)}


def test_same_term_present_and_absent_is_an_error():
    r = rec(phenotypes=[
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), evidence=EV),
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), excluded=True, evidence=EV),
    ])
    assert "polarity_contradiction" in codes(r)


def test_specific_present_while_general_absent_is_an_error():
    """If a focal seizure is present, 'seizure' cannot be absent."""
    r = rec(phenotypes=[
        PhenotypeAssertion(term=OntologyClass(id="HP:0007359"), evidence=EV),
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), excluded=True, evidence=EV),
    ])
    assert "dag_polarity_contradiction" in codes(r)


def test_consistent_record_has_no_violations():
    r = rec(phenotypes=[
        PhenotypeAssertion(term=OntologyClass(id="HP:0007359"), evidence=EV),
        PhenotypeAssertion(term=OntologyClass(id="HP:0000252"), excluded=True, evidence=EV),
    ])
    assert not codes(r)


def test_onset_after_last_encounter_is_an_error():
    r = rec(
        subject=Subject(age_at_last_encounter=TimeElement(iso8601duration="P5Y"), evidence=EV),
        phenotypes=[PhenotypeAssertion(term=OntologyClass(id="HP:0001250"),
                                       onset=TimeElement(iso8601duration="P20Y"), evidence=EV)],
    )
    assert "onset_after_last_encounter" in codes(r)


def test_iso_duration_parsing_handles_weeks():
    """Gestational ages are written PnW; returning None would skip the check."""
    assert constraints._iso_to_days("P32W") == pytest.approx(224.0)
    assert constraints._iso_to_days("P1Y6M") == pytest.approx(547.89, abs=0.1)
    assert constraints._iso_to_days("garbage") is None


def test_gene_not_linked_to_diagnosis_warns_but_does_not_error():
    r = rec(
        diagnoses=[DiagnosisAssertion(disease=OntologyClass(id="OMIM:162200"), evidence=EV)],
        variants=[VariantAssertion(gene=OntologyClass(id=STORE.gene_id("STXBP1")), evidence=EV)],
    )
    vs = {v.code: v.severity for v in constraints.check(STORE, r)}
    # A novel gene-disease association is how discoveries look, so WARN not ERROR.
    assert vs.get("gene_disease_mismatch") == constraints.SEV_WARN


def test_annotate_flags_and_lowers_confidence_without_dropping_the_record():
    r = rec(phenotypes=[
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), evidence=EV),
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), excluded=True, evidence=EV),
    ])
    out = constraints.annotate(STORE, r)
    assert out.qa_flags and out.confidence is not None and out.confidence < 1.0
    assert len(out.phenotypes) == 2      # never silently deleted


def test_consensus_weights_by_agreement():
    def one(terms):
        return rec(phenotypes=[
            PhenotypeAssertion(term=OntologyClass(id=t), evidence=EV) for t in terms
        ])

    result = consensus.merge([
        one(["HP:0001250", "HP:0000252"]),
        one(["HP:0001250", "HP:0000252"]),
        one(["HP:0001250"]),
    ])
    assert result.agreement["HP:0001250"] == 1.0
    assert result.agreement["HP:0000252"] == pytest.approx(2 / 3, abs=1e-4)  # stored rounded to 4dp
    assert result.merged.confidence is not None
