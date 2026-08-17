"""Schema round-trip and the provenance-or-null rule."""
from __future__ import annotations

import pytest

from rdcd.schema import (
    CaseRecord, DiagnosisAssertion, Evidence, OntologyClass,
    PhenotypeAssertion, Section, SourceDoc, Subject, TimeElement, VariantAssertion,
)


def ev(**kw):
    return Evidence(source_id="PMID:1", extractor="test", **kw)


def make(**kw) -> CaseRecord:
    base = dict(id="case-1", source=SourceDoc(pmid="1", quotes_permitted=True))
    base.update(kw)
    return CaseRecord(**base)


def test_curie_required():
    with pytest.raises(ValueError):
        OntologyClass(id="not-a-curie")


def test_evidence_span_must_be_ordered():
    with pytest.raises(ValueError):
        Evidence(source_id="PMID:1", start=100, end=50)


def test_observed_and_excluded_are_separate_views():
    rec = make(phenotypes=[
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), evidence=[ev()]),
        PhenotypeAssertion(term=OntologyClass(id="HP:0000252"), excluded=True, evidence=[ev()]),
    ])
    assert rec.observed_hpo == {"HP:0001250"}
    assert rec.excluded_hpo == {"HP:0000252"}


def test_unprovenanced_assertions_are_reported_and_dropped():
    rec = make(phenotypes=[
        PhenotypeAssertion(term=OntologyClass(id="HP:0001250"), evidence=[ev()]),
        PhenotypeAssertion(term=OntologyClass(id="HP:0000252")),  # no evidence
    ])
    assert len(rec.unprovenanced()) == 1
    clean, dropped = rec.enforce_provenance()
    assert len(dropped) == 1
    assert clean.observed_hpo == {"HP:0001250"}
    assert not clean.unprovenanced()


def test_phenopacket_round_trip_preserves_the_scored_fields():
    rec = make(
        subject=Subject(id="II-2", sex="FEMALE",
                        age_at_last_encounter=TimeElement(iso8601duration="P7Y"), evidence=[ev()]),
        phenotypes=[
            PhenotypeAssertion(term=OntologyClass(id="HP:0001250", label="Seizure"), evidence=[ev()]),
            PhenotypeAssertion(term=OntologyClass(id="HP:0000252"), excluded=True, evidence=[ev()]),
        ],
        diagnoses=[DiagnosisAssertion(disease=OntologyClass(id="OMIM:612164"), evidence=[ev()])],
        variants=[VariantAssertion(gene=OntologyClass(id="HGNC:11444", label="STXBP1"),
                                   hgvs_c="NM_001032221.6:c.1adelG", evidence=[ev()])],
    )
    back = CaseRecord.from_phenopacket(rec.to_phenopacket())
    assert back.observed_hpo == rec.observed_hpo
    assert back.excluded_hpo == rec.excluded_hpo
    assert back.disease_ids == rec.disease_ids
    assert back.gene_symbols == rec.gene_symbols
    assert back.subject.sex == "FEMALE"


def test_quote_is_withheld_when_licence_forbids_redistribution():
    src = SourceDoc(pmid="1", quotes_permitted=False)
    assert src.curie == "PMID:1"
    # Offsets are always recordable; the quote is the licensed part.
    e = Evidence(source_id=src.curie, start=0, end=5, quote=None, section=Section.BODY)
    assert e.locator == "PMID:1#body:0-5"
