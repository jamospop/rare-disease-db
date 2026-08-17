"""OBO parsing, cross-references, grounding and negation."""
from __future__ import annotations

import pytest

from rdcd.ontology.grounding import Grounder, GeneGrounder, find_hgvs
from rdcd.ontology.store import STORE


@pytest.fixture(scope="module")
def store():
    return STORE


def test_obo_modifiers_do_not_leak_into_xrefs(store):
    """MONDO writes `xref: OMIM:1 {source="MONDO:equivalentTo"}`.

    Keeping the trailing brace block in the CURIE silently broke every OMIM
    lookup, so this is a regression test for a real bug.
    """
    idx = store.mondo.xref_index(("OMIM:",))
    assert "OMIM:162200" in idx
    assert all(" " not in k for k in list(idx)[:200])


def test_omim_to_mondo_normalisation(store):
    assert store.normalize_disease("OMIM:162200") == "MONDO:0018975"
    assert store.disease_label("OMIM:162200") == "neurofibromatosis type 1"


def test_gene_symbol_history_resolves(store):
    assert store.gene_id("STXBP1") == "HGNC:11444"
    # FOG2 is a historical name for ZFPM2; papers use both.
    assert store.canonical_gene_symbol("FOG2") == "ZFPM2"


def test_information_content_increases_with_specificity(store):
    root = store.information_content("HP:0000118")     # Phenotypic abnormality
    general = store.information_content("HP:0001250")  # Seizure
    specific = store.information_content("HP:0007359")  # Focal-onset seizure
    assert root < general < specific


def test_dag_relationships(store):
    assert store.hpo.is_ancestor_of("HP:0001250", "HP:0007359")
    assert store.hpo.path_distance("HP:0001250", "HP:0007359") == 1


def test_grounding_detects_negation(store):
    g = Grounder(store)
    hits = {m.term_id: m for m in g.find("Seizures were present but no hydrocephalus.")}
    assert hits["HP:0001250"].negated is False
    assert hits["HP:0000238"].negated is True


def test_negation_does_not_cross_a_sentence_boundary(store):
    g = Grounder(store)
    hits = {m.term_id: m for m in g.find(
        "There was no evidence of retinal detachment. Hypertrichosis was absent.")}
    # Both negated, but Hypertrichosis by its own post-trigger, not the leaked one.
    assert hits["HP:0000998"].negated is True
    assert hits["HP:0000998"].negation_cue == "was absent"


def test_multiword_related_synonyms_recover_common_phrases(store):
    assert "hearing loss" in Grounder(store, multiword_related=True).phrases
    assert "hearing loss" not in Grounder(store, multiword_related=False).phrases


def test_disease_abbreviations_are_not_mistaken_for_genes(store):
    found = {m.phrase for m in GeneGrounder(store).find(
        "The patient had LCA and KS; a STXBP1 variant was identified.")}
    assert "STXBP1" in found
    assert "LCA" not in found and "KS" not in found


def test_grounding_is_restricted_to_phenotypic_abnormality(store):
    """Regression test for the largest single precision bug found.

    HPO contains real terms that are not phenotypes: modifiers ("Bilateral"),
    status terms ("Affected"), inheritance modes, and frequency terms. A
    phenopacket's phenotypicFeatures hold only descendants of HP:0000118 -
    verified at 100.00% of 90,549 gold terms - so grounding into the other
    branches produced 31.9% guaranteed-false-positive assertions. Fixing this
    moved Track SINGLE graded F1 from 0.4404 to 0.5581.
    """
    restricted = Grounder(store)                 # default root = HP:0000118
    unrestricted = Grounder(store, root=None)
    for junk in ("Affected", "Bilateral", "Autosomal dominant inheritance"):
        assert restricted.find(junk) == [], junk
        assert unrestricted.find(junk) != [], junk
    # Real phenotypes are unaffected.
    for real, expected in (("microcephaly", "HP:0000252"), ("seizures", "HP:0001250")):
        assert [m.term_id for m in restricted.find(real)] == [expected]


def test_gene_and_disease_grounders_are_not_root_restricted(store):
    """The HPO root filter must not leak into MONDO or gene grounding."""
    assert Grounder(store, which="mondo").root is None
    assert Grounder(store, which="mondo").find("neurofibromatosis type 1")


def test_hgvs_extraction():
    kinds = {k for k, *_ in find_hgvs("NM_138961.3:c.35T>A (p.Leu12Ter)")}
    assert kinds == {"hgvs.c", "hgvs.p"}
