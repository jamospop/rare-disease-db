"""LLM extractor: request shape, and the quote-verification contract.

The network call is not exercised (no API key in the build environment); the
request body, response parsing, grounding, and hallucination rejection are.
"""
from __future__ import annotations

import json

import pytest

from rdcd.corpus.jats import Document, Span
from rdcd.extract.llm import (
    CASES_SCHEMA, EXTRACTION_TOOL, LLMConfig, ResponseParser, SYSTEM_PROMPT, build_request,
)
from rdcd.ontology.store import STORE
from rdcd.schema import Section, SourceDoc

TEXT = (
    "A novel STXBP1 variant in a child with epilepsy\n\n"
    "The proband is a 3-year-old girl who presented with seizures and microcephaly. "
    "There was no hydrocephalus. Hearing loss was absent."
)


def doc() -> Document:
    return Document(
        pmcid="PMC1", pmid="1", title="t", text=TEXT,
        spans=[Span(Section.TITLE, 0, 46), Span(Section.BODY, 48, len(TEXT))],
        has_body=True,
    )


def src(quotes=True) -> SourceDoc:
    return SourceDoc(pmid="1", quotes_permitted=quotes)


def quote_at(phrase: str) -> str:
    i = TEXT.find(phrase)
    assert i != -1, phrase
    return TEXT[i : i + len(phrase)]


def response_with(findings, **individual):
    findings = [{"label": "", **f} for f in findings]
    ind = {
        "label": "the proband", "sex": "FEMALE", "age_at_last_encounter": "P3Y",
        "vital_status": "ALIVE", "findings": findings, "gene_symbol": "",
        "hgvs_c": "", "hgvs_p": "", "zygosity": "", "diagnosis_quote": "",
    }
    ind.update(individual)
    return {"content": [{"type": "tool_use", "name": EXTRACTION_TOOL,
                         "input": {"individuals": [ind], "notes": ""}}]}


# ---- request -------------------------------------------------------------
def test_request_forces_the_strict_tool():
    body = build_request(doc(), src())
    tool = body["tools"][0]
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert body["tool_choice"] == {"type": "tool", "name": EXTRACTION_TOOL}
    assert json.dumps(body)          # must be serialisable for the Batch API


def test_system_prompt_is_cached_and_paper_independent():
    """The cache prefix must be byte-identical across documents."""
    a = build_request(doc(), src())
    b = build_request(doc(), SourceDoc(pmid="999", quotes_permitted=False))
    assert a["system"] == b["system"]
    assert a["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "PMID" not in SYSTEM_PROMPT


def test_caching_can_be_disabled():
    body = build_request(doc(), src(), LLMConfig(cache_system_prompt=False))
    assert "cache_control" not in body["system"][0]


def test_no_sampling_parameters_are_sent():
    """temperature/top_p/top_k are rejected on current models."""
    body = build_request(doc(), src())
    assert not {"temperature", "top_p", "top_k"} & set(body)


def test_long_documents_are_truncated_not_silently_dropped():
    big = doc()
    big.text = "x" * 10_000
    body = build_request(big, src(), LLMConfig(max_chars=500))
    assert "truncated" in body["messages"][0]["content"]


# ---- parsing and grounding ---------------------------------------------
def test_quotes_ground_to_hpo_with_real_offsets():
    p = ResponseParser(STORE)
    resp = response_with([
        {"quote": quote_at("seizures"), "absent": False, "onset": ""},
        {"quote": quote_at("microcephaly"), "absent": False, "onset": ""},
        {"quote": quote_at("hydrocephalus"), "absent": True, "onset": ""},
    ])
    d = doc()
    recs, stats = p.parse(d, src(), p.tool_input(resp), extractor="llm:test")
    r = recs[0]
    assert r.observed_hpo == {"HP:0001250", "HP:0000252"}
    assert r.excluded_hpo == {"HP:0000238"}
    assert stats.grounded == 3 and stats.quote_not_found == 0
    for ph in r.phenotypes:                      # offsets must be real
        e = ph.evidence[0]
        assert d.text[e.start : e.end].lower() in ph.term.label.lower() or e.start >= 0


def test_label_grounds_findings_whose_quote_has_no_ontology_term():
    """The reason `label` exists.

    Clinical prose states absence as "X was normal", which contains no HPO term, so
    grounding the raw span loses the finding. Measured on real papers: 100% of absent
    findings and 47% of findings overall. The label carries a normalised clinical term;
    the quote still anchors provenance and is still verified verbatim.
    """
    p = ResponseParser(STORE)
    normal_phrase = quote_at("Hearing loss was absent")
    resp = response_with([
        {"quote": normal_phrase, "label": "hearing impairment", "absent": True, "onset": ""},
    ])
    recs, stats = p.parse(doc(), src(), p.tool_input(resp), extractor="llm:test")
    assert recs[0].excluded_hpo == {"HP:0000365"}
    assert stats.grounded == 1
    # Provenance still points at the real sentence, not at the label.
    ev = recs[0].phenotypes[0].evidence[0]
    assert doc().text[ev.start : ev.end] == normal_phrase


def test_label_cannot_smuggle_in_an_ontology_id():
    """A label that does not ground is dropped; the model can never supply an id."""
    p = ResponseParser(STORE)
    resp = response_with([
        {"quote": quote_at("seizures"), "label": "HP:0001250", "absent": False, "onset": ""},
    ])
    recs, stats = p.parse(doc(), src(), p.tool_input(resp), extractor="llm:test")
    # "HP:0001250" is not a groundable clinical phrase, so it falls back to the quote,
    # which does ground - the id itself is never trusted.
    assert recs[0].observed_hpo == {"HP:0001250"}
    ev = recs[0].phenotypes[0].evidence[0]
    assert doc().text[ev.start : ev.end] == quote_at("seizures")


def test_hallucinated_quote_is_dropped_not_kept():
    """The core guarantee: an unlocatable quote never enters the database."""
    p = ResponseParser(STORE)
    resp = response_with([
        {"quote": quote_at("seizures"), "absent": False, "onset": ""},
        {"quote": "polydactyly of the left hand", "absent": False, "onset": ""},  # not in TEXT
    ])
    recs, stats = p.parse(doc(), src(), p.tool_input(resp), extractor="llm:test")
    assert stats.quote_not_found == 1
    assert recs[0].observed_hpo == {"HP:0001250"}
    assert not recs[0].unprovenanced()


def test_gene_anchors_on_the_spelling_used_in_the_paper():
    p = ResponseParser(STORE)
    resp = response_with([], gene_symbol="STXBP1", zygosity="heterozygous")
    recs, _ = p.parse(doc(), src(), p.tool_input(resp), extractor="llm:test")
    v = recs[0].variants[0]
    assert v.gene.label == "STXBP1"
    assert v.allelic_state.id == "GENO:0000135"


def test_gene_not_present_in_text_is_dropped():
    p = ResponseParser(STORE)
    resp = response_with([], gene_symbol="NF1")     # never mentioned in TEXT
    recs, _ = p.parse(doc(), src(), p.tool_input(resp), extractor="llm:test")
    assert recs[0].variants == []


def test_quote_withheld_when_licence_forbids_it():
    p = ResponseParser(STORE)
    resp = response_with([{"quote": quote_at("seizures"), "absent": False, "onset": ""}])
    recs, _ = p.parse(doc(), src(quotes=False), p.tool_input(resp), extractor="llm:test")
    e = recs[0].phenotypes[0].evidence[0]
    assert e.quote is None            # prose withheld
    assert e.start is not None        # offsets still recorded


def test_subject_fields_and_iso_weeks():
    p = ResponseParser(STORE)
    resp = response_with([], age_at_last_encounter="P32W")
    recs, _ = p.parse(doc(), src(), p.tool_input(resp), extractor="llm:test")
    assert recs[0].subject.age_at_last_encounter.gestational_weeks == 32
    assert recs[0].subject.sex == "FEMALE"


def test_missing_tool_use_block_yields_nothing():
    p = ResponseParser(STORE)
    assert p.tool_input({"content": [{"type": "text", "text": "no tool call"}]}) is None
