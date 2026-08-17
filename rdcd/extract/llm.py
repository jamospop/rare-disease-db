"""LLM extractor. Quotes in, ontology IDs out.

The central design choice, and the reason this file is worth reading:

  **The model never emits an ontology ID.** It returns the verbatim phrase it
  saw, whether that phrase was negated, and which individual it belongs to.
  We ground the phrase to HPO/MONDO/HGNC ourselves.

Why: a language model asked for "HP:0001250" will sometimes produce a
well-formed, plausible, wrong identifier, and nothing downstream can tell. A
quote, by contrast, is mechanically checkable - either it appears in the source
document or it does not. That single constraint gives us:

  * provenance-or-null for free: an unlocatable quote is dropped, so every
    surviving assertion has a real character offset (rdcd.qa.provenance);
  * no ID hallucination, because IDs come from the ontology, not the model;
  * a legible failure mode: when extraction is wrong you see which phrase
    fired, exactly as with the dictionary baseline.

What the model is actually for is the part dictionaries cannot do: deciding
which individual a finding belongs to in a multi-individual cohort paper, and
judging negation and hypotheticals in real clinical prose.

Cost control, in order of MEASURED impact (129-paper dev split, claude-opus-5 list price):
  1. Batch API - 50% off ($13.57 -> $6.78). An offline corpus job, so the up-to-24h
     turnaround costs nothing. By far the biggest lever.
  2. Effort, tuned per run rather than left at the default.
  3. Prompt caching - worth only $0.34 here (2.5%). The system prompt is ~585 tokens
     against ~770k tokens of document text, and documents are unique per call so they
     cannot be cached. Kept because it is free and would matter if the prompt grew,
     but it is not a design pillar; claiming otherwise was an unmeasured assumption.
     NOTE: the minimum cacheable prefix is 512 tokens on Opus 5 but 1024 on most other
     models, so at 585 tokens this caches on Opus 5 and would SILENTLY stop caching
     elsewhere. Watch cache_read_input_tokens.

Status: written against the current Messages API contract but NOT executed
live - no API key was available in the environment where this was built. The
schema, prompt assembly, response parsing, and grounding are covered by offline
tests (tests/test_llm_extractor.py); the network call is not.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from ..corpus.jats import Document
from ..ontology.grounding import Grounder, GeneGrounder
from ..ontology.store import OntologyStore
from ..schema import (
    CaseRecord,
    DiagnosisAssertion,
    Evidence,
    OntologyClass,
    PhenotypeAssertion,
    Section,
    SourceDoc,
    Subject,
    VariantAssertion,
)

MODEL = "claude-opus-5"
EXTRACTION_TOOL = "record_cases"

# Effort: this is a long-horizon structured-extraction task over full papers.
# `high` is the documented starting point for intelligence-sensitive work; the
# calibration script sweeps medium/high/xhigh on the dev split.
DEFAULT_EFFORT = "high"


# ---------------------------------------------------------------------------
# The tool schema. strict=True guarantees the arguments validate exactly, so we
# never write defensive parsing for a shape the API already enforces.
# ---------------------------------------------------------------------------
FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quote": {
            "type": "string",
            "description": (
                "The exact substring from the document naming this finding, copied "
                "character for character. Must be short - the clinical phrase only, "
                "not the whole sentence. If you cannot copy it exactly, omit the finding."
            ),
        },
        "label": {
            "type": "string",
            "description": (
                "The finding as a standard clinical term, normalised - e.g. quote "
                "'the heart was determined to be grossly normal' -> label "
                "'abnormality of the heart'; quote 'IQ of 62' -> label "
                "'intellectual disability'. Name the ABNORMALITY even when the source "
                "states it was absent; `absent` carries the polarity. Do NOT output an "
                "ontology identifier - text only. Leave empty to fall back to grounding "
                "the quote itself."
            ),
        },
        "absent": {
            "type": "boolean",
            "description": (
                "true when the source states this finding was looked for and NOT "
                "found (explicitly absent/normal/ruled out). false when present. "
                "Roughly 60% of curated findings are absent, so read negation carefully."
            ),
        },
        "onset": {
            "type": "string",
            "description": "Age at onset as ISO-8601 duration (P3Y, P2M) if stated, else empty.",
        },
    },
    "required": ["quote", "label", "absent", "onset"],
    "additionalProperties": False,
}

INDIVIDUAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "description": (
                "How the source identifies this person: 'Patient 3', 'II-2', "
                "'the proband'. Use the source's own label so a curator can find them."
            ),
        },
        "sex": {"type": "string", "enum": ["MALE", "FEMALE", "OTHER_SEX", "UNKNOWN_SEX"]},
        "age_at_last_encounter": {
            "type": "string",
            "description": "ISO-8601 duration (P32Y, P6M, P32W for gestational) if stated, else empty.",
        },
        "vital_status": {"type": "string", "enum": ["ALIVE", "DECEASED", "UNKNOWN_STATUS"]},
        "findings": {"type": "array", "items": FINDING_SCHEMA},
        "gene_symbol": {
            "type": "string",
            "description": "Gene symbol as written in the paper, e.g. STXBP1. Empty if none.",
        },
        "hgvs_c": {"type": "string", "description": "cDNA HGVS as written, else empty."},
        "hgvs_p": {"type": "string", "description": "Protein HGVS as written, else empty."},
        "zygosity": {
            "type": "string",
            "enum": ["", "heterozygous", "homozygous", "hemizygous", "compound heterozygous"],
        },
        "diagnosis_quote": {
            "type": "string",
            "description": "Verbatim disease name as written in the paper. Empty if none stated.",
        },
    },
    "required": [
        "label", "sex", "age_at_last_encounter", "vital_status", "findings",
        "gene_symbol", "hgvs_c", "hgvs_p", "zygosity", "diagnosis_quote",
    ],
    "additionalProperties": False,
}

CASES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "individuals": {
            "type": "array",
            "description": (
                "One entry per individual PATIENT described with their own clinical "
                "data. Do not create entries for unaffected relatives mentioned only "
                "in passing, for cohorts described in aggregate, or for patients from "
                "other papers discussed in the introduction or discussion."
            ),
            "items": INDIVIDUAL_SCHEMA,
        },
        "notes": {
            "type": "string",
            "description": "Anything a human curator should know about this paper. Empty if nothing.",
        },
    },
    "required": ["individuals", "notes"],
    "additionalProperties": False,
}

# The system prompt is deliberately long and byte-stable: it is identical for
# every paper in the corpus, so it is the ideal cache prefix. Never interpolate
# per-paper content into it - that would invalidate the cache on every request.
SYSTEM_PROMPT = """\
You extract structured case data from rare-disease publications for an open research database.

Your output is graded against expert-curated GA4GH phenopackets, so match what a \
Human Phenotype Ontology curator would record.

Rules, in order of importance:

1. QUOTE EXACTLY. Every finding needs the verbatim substring from the document. \
Copy it character for character - do not normalise spelling, expand abbreviations, \
fix hyphenation, or tidy whitespace. A quote that is not a literal substring of the \
document is discarded, and the finding is lost. Prefer the shortest phrase that names \
the finding.

2. LABEL THE ABNORMALITY, NOT THE WORDING. Alongside each quote, give `label`: the finding as a standard clinical term. "the heart was grossly normal" -> label "abnormality of the heart"; "IQ of 62" -> label "intellectual disability". Always name the abnormality, even for findings the source says were ABSENT - `absent` carries the polarity, not the label. Without this, absent findings are unrecoverable, because prose states them as "X was normal" and there is no clinical term in that span to match.

3. NEGATION IS HALF THE SIGNAL. Curators record explicitly absent findings as \
carefully as present ones, and absent findings outnumber present ones in the gold \
data. "no seizures", "normal hearing", "renal ultrasound was unremarkable", \
"ruled out" are all findings with absent=true. Do not silently drop them. But a \
finding never mentioned at all is not absent - omit it entirely.

4. ONE ENTRY PER PATIENT. Cohort papers describe many individuals, often in tables \
where each column or row is one person. Attribute each finding to the individual it \
belongs to. If a finding is reported for the cohort as a whole and cannot be assigned \
to an individual, leave it out and say so in notes.

5. THIS PAPER'S PATIENTS ONLY. Introductions and discussions describe previously \
published cases. Those are not this paper's patients. Extract only individuals whose \
clinical data this paper reports.

6. OMIT RATHER THAN GUESS. Empty string for anything not stated. Do not infer sex \
from a name, do not convert "young adult" into a number, do not upgrade a suspected \
diagnosis into a confirmed one. A missing field costs far less than a wrong one.

Do not output ontology identifiers of any kind - no HP:, MONDO:, OMIM: codes. \
Quote the text; identifiers are assigned downstream.
"""


@dataclass
class LLMConfig:
    model: str = MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = 16000
    thinking_adaptive: bool = True
    cache_system_prompt: bool = True
    max_chars: int = 400_000  # ~100k tokens of document text
    temperature: None = None  # sampling params are rejected on current models


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
def build_request(doc: Document, source: SourceDoc, cfg: LLMConfig | None = None) -> dict[str, Any]:
    """Build the Messages API request body for one document.

    Returned as a plain dict so the same body can be sent one-off or embedded in
    a Batch API request without a second code path.
    """
    cfg = cfg or LLMConfig()
    text = doc.text[: cfg.max_chars]
    truncated = len(doc.text) > cfg.max_chars

    system: list[dict[str, Any]] = [{"type": "text", "text": SYSTEM_PROMPT}]
    if cfg.cache_system_prompt:
        # Cache the tools+system prefix. This block is byte-identical across the
        # whole corpus, so after the first paper it is a cache read.
        system[0]["cache_control"] = {"type": "ephemeral"}

    user = (
        f"Document: {source.curie}\n"
        f"{'NOTE: text was truncated to fit the context window.' if truncated else ''}\n\n"
        f"<document>\n{text}\n</document>\n\n"
        "Record every individual patient this paper reports clinical data for."
    )

    body: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "system": system,
        "tools": [
            {
                "name": EXTRACTION_TOOL,
                "description": (
                    "Record the individual patients described in this publication, with "
                    "verbatim quotes for every clinical finding."
                ),
                "strict": True,
                "input_schema": CASES_SCHEMA,
            }
        ],
        "tool_choice": {"type": "tool", "name": EXTRACTION_TOOL},
        "messages": [{"role": "user", "content": user}],
    }
    return body


def _extra_body(cfg: LLMConfig) -> dict[str, Any]:
    """Params newer than the installed SDK's typed signature.

    Passed via extra_body so this module works against both an older SDK (where
    `thinking`/`output_config` are not keyword arguments) and a current one.
    """
    extra: dict[str, Any] = {"output_config": {"effort": cfg.effort}}
    if cfg.thinking_adaptive:
        extra["thinking"] = {"type": "adaptive"}
    return extra


# ---------------------------------------------------------------------------
# Response -> CaseRecord, with grounding and quote verification
# ---------------------------------------------------------------------------
@dataclass
class GroundingStats:
    findings_returned: int = 0
    quote_not_found: int = 0
    quote_ungroundable: int = 0
    grounded: int = 0
    individuals: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class ResponseParser:
    """Turn tool arguments into CaseRecords, dropping anything unverifiable."""

    def __init__(self, store: OntologyStore):
        self.store = store
        self.pheno = Grounder(store)
        self.disease = Grounder(store, which="mondo", multiword_related=False)
        self.genes = GeneGrounder(store)

    @staticmethod
    def tool_input(response: Any) -> dict[str, Any] | None:
        """Pull the tool arguments out of a Messages API response."""
        content = response["content"] if isinstance(response, dict) else response.content
        for block in content:
            btype = block["type"] if isinstance(block, dict) else block.type
            if btype != "tool_use":
                continue
            name = block["name"] if isinstance(block, dict) else block.name
            if name != EXTRACTION_TOOL:
                continue
            raw = block["input"] if isinstance(block, dict) else block.input
            return json.loads(raw) if isinstance(raw, str) else raw
        return None

    def _locate(self, doc: Document, quote: str) -> tuple[int, int] | None:
        """Find a quote in the document. Exact first, then whitespace-tolerant.

        Whitespace normalisation is the only latitude given: models reliably
        reproduce wording but not line breaks inside a table cell. Everything
        else must match, because that is what makes the offset trustworthy.
        """
        q = quote.strip()
        if not q:
            return None
        idx = doc.text.find(q)
        if idx != -1:
            return idx, idx + len(q)
        squashed = " ".join(q.split())
        idx = doc.text.find(squashed)
        if idx != -1:
            return idx, idx + len(squashed)
        return None

    def parse(
        self, doc: Document, source: SourceDoc, args: dict[str, Any], *, extractor: str
    ) -> tuple[list[CaseRecord], GroundingStats]:
        stats = GroundingStats()
        records: list[CaseRecord] = []

        for i, ind in enumerate(args.get("individuals") or []):
            stats.individuals += 1
            phenotypes: list[PhenotypeAssertion] = []

            for f in ind.get("findings") or []:
                stats.findings_returned += 1
                quote = (f.get("quote") or "").strip()
                span = self._locate(doc, quote)
                if span is None:
                    # Unverifiable quote. Dropped, not downgraded: this is the
                    # provenance-or-null rule doing its job.
                    stats.quote_not_found += 1
                    continue
                start, end = span
                # Ground the normalised label if given, else the quote span itself.
                # Clinical prose states negation as "X was normal", which contains no
                # HPO term at all, so grounding the raw span loses essentially every
                # absent finding (measured: 100% of them, and 47% of findings overall).
                # The label fixes that without letting the model emit identifiers: it
                # supplies text, the ontology supplies the id, and an ungroundable
                # label is still dropped.
                label = (f.get("label") or "").strip()
                term = None
                for candidate in (label, quote):
                    if not candidate:
                        continue
                    hits = self.pheno.find(candidate)
                    term = next(
                        (t for t in (self.store.hpo.normalize(h.term_id) for h in hits) if t),
                        None,
                    )
                    if term:
                        break
                if term is None:
                    stats.quote_ungroundable += 1
                    continue
                stats.grounded += 1
                sect, _ = doc.section_at(start)
                onset = (f.get("onset") or "").strip()
                phenotypes.append(
                    PhenotypeAssertion(
                        term=OntologyClass(id=term, label=self.store.hpo.label(term)),
                        excluded=bool(f.get("absent")),
                        onset=_time_element(onset),
                        grounded_from=(label or quote),
                        evidence=[
                            Evidence(
                                source_id=source.curie,
                                section=sect,
                                start=start,
                                end=end,
                                quote=quote if source.quotes_permitted else None,
                                extractor=extractor,
                            )
                        ],
                    )
                )

            variants = self._variants(doc, source, ind, extractor)
            diagnoses = self._diagnoses(doc, source, ind, extractor)

            records.append(
                CaseRecord(
                    id=f"{source.curie.replace(':', '_')}_individual_{i + 1}",
                    source=source,
                    subject=Subject(
                        id=(ind.get("label") or f"individual_{i + 1}"),
                        sex=ind.get("sex") or "UNKNOWN_SEX",
                        age_at_last_encounter=_time_element(ind.get("age_at_last_encounter") or ""),
                        vital_status=ind.get("vital_status") or "UNKNOWN_STATUS",
                        evidence=[Evidence(source_id=source.curie, extractor=extractor)],
                    ),
                    phenotypes=phenotypes,
                    diagnoses=diagnoses,
                    variants=variants,
                    extractors=[extractor],
                )
            )
        return records, stats

    def _variants(
        self, doc: Document, source: SourceDoc, ind: dict, extractor: str
    ) -> list[VariantAssertion]:
        sym = (ind.get("gene_symbol") or "").strip()
        if not sym:
            return []
        hid = self.store.gene_id(sym)
        if not hid:
            return []
        # Anchor on whichever spelling the paper actually uses. A paper may write
        # the approved symbol, a historical symbol, or an alias (this corpus has
        # ZFPM2 written as FOG2), so try every name HGNC knows for the gene
        # before giving up. Anchoring only on the approved symbol silently drops
        # correctly-identified genes.
        anchor = self._locate(doc, sym)
        if anchor is None:
            rec = self.store.genes.get(hid)
            names = [rec.symbol, *rec.aliases, *rec.prev_symbols] if rec else []
            for name in names:
                anchor = self._locate(doc, name)
                if anchor is not None:
                    sym = name  # quote the spelling that is actually in the text
                    break
        if anchor is None:
            anchor = self._locate(doc, (ind.get("hgvs_c") or "").strip())
        if anchor is None:
            return []
        start, end = anchor
        sect, _ = doc.section_at(start)
        zyg = (ind.get("zygosity") or "").strip()
        return [
            VariantAssertion(
                gene=OntologyClass(id=hid, label=self.store.gene_symbol(hid)),
                hgvs_c=(ind.get("hgvs_c") or "").strip() or None,
                hgvs_p=(ind.get("hgvs_p") or "").strip() or None,
                allelic_state=(
                    OntologyClass(id=_GENO[zyg], label=zyg) if zyg in _GENO else None
                ),
                evidence=[
                    Evidence(
                        source_id=source.curie, section=sect, start=start, end=end,
                        quote=(sym if source.quotes_permitted else None), extractor=extractor,
                    )
                ],
            )
        ]

    def _diagnoses(
        self, doc: Document, source: SourceDoc, ind: dict, extractor: str
    ) -> list[DiagnosisAssertion]:
        quote = (ind.get("diagnosis_quote") or "").strip()
        if not quote:
            return []
        span = self._locate(doc, quote)
        if span is None:
            return []
        hits = self.disease.find(quote)
        mid = next((m for m in (self.store.mondo.normalize(h.term_id) for h in hits) if m), None)
        if not mid:
            return []
        start, end = span
        sect, _ = doc.section_at(start)
        return [
            DiagnosisAssertion(
                disease=OntologyClass(id=mid, label=self.store.mondo.label(mid)),
                stated_in_abstract=sect in (Section.TITLE, Section.ABSTRACT),
                evidence=[
                    Evidence(
                        source_id=source.curie, section=sect, start=start, end=end,
                        quote=(quote if source.quotes_permitted else None), extractor=extractor,
                    )
                ],
            )
        ]


_GENO = {
    "heterozygous": "GENO:0000135",
    "homozygous": "GENO:0000136",
    "hemizygous": "GENO:0000134",
    "compound heterozygous": "GENO:0000402",
}


def _time_element(iso: str):
    from ..schema import TimeElement

    iso = (iso or "").strip()
    if not iso.startswith("P"):
        return None
    if iso.endswith("W") and iso[1:-1].isdigit():
        return TimeElement(gestational_weeks=int(iso[1:-1]))
    return TimeElement(iso8601duration=iso)


# ---------------------------------------------------------------------------
# Live extractor (one request per document)
# ---------------------------------------------------------------------------
class LLMExtractor:
    """Single-document extractor. For corpus scale use submit_batch instead."""

    def __init__(self, store: OntologyStore, cfg: LLMConfig | None = None, client: Any = None):
        self.store = store
        self.cfg = cfg or LLMConfig()
        self.parser = ResponseParser(store)
        self._client = client
        self.last_stats: GroundingStats | None = None
        self.last_usage: dict | None = None

    @property
    def name(self) -> str:
        return f"llm:{self.cfg.model}:effort={self.cfg.effort}"

    @property
    def client(self):
        if self._client is None:
            import anthropic  # imported lazily: unused when running the baseline

            self._client = anthropic.Anthropic()
        return self._client

    def extract(self, doc: Document, source: SourceDoc) -> list[CaseRecord]:
        body = build_request(doc, source, self.cfg)
        response = self.client.messages.create(**body, extra_body=_extra_body(self.cfg))
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            }
        args = self.parser.tool_input(response)
        if args is None:
            self.last_stats = GroundingStats()
            return []
        records, stats = self.parser.parse(doc, source, args, extractor=self.name)
        self.last_stats = stats
        return records


# ---------------------------------------------------------------------------
# Batch API: the corpus-scale path
# ---------------------------------------------------------------------------
@dataclass
class BatchJob:
    batch_id: str
    custom_ids: list[str] = field(default_factory=list)


def submit_batch(
    items: Iterable[tuple[str, Document, SourceDoc]],
    *,
    cfg: LLMConfig | None = None,
    client: Any = None,
) -> BatchJob:
    """Submit up to 100k documents as one batch at half the per-token price.

    Batch is the right shape for this workload: a corpus pass is offline, so the
    up-to-24-hour turnaround costs nothing and halves the bill. The shared system
    prompt still caches within the batch.
    """
    cfg = cfg or LLMConfig()
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    requests, ids = [], []
    for custom_id, doc, source in items:
        body = build_request(doc, source, cfg)
        body.update(_extra_body(cfg))
        requests.append({"custom_id": custom_id, "params": body})
        ids.append(custom_id)
    batch = client.messages.batches.create(requests=requests)
    return BatchJob(batch_id=batch.id, custom_ids=ids)


def collect_batch(
    job: BatchJob,
    resolve: Any,
    store: OntologyStore,
    *,
    extractor_name: str,
    client: Any = None,
) -> tuple[dict[str, list[CaseRecord]], dict[str, GroundingStats]]:
    """Read finished batch results. `resolve(custom_id) -> (Document, SourceDoc)`.

    Results arrive in any order, so everything is keyed by custom_id, never by
    position.
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    parser = ResponseParser(store)
    out: dict[str, list[CaseRecord]] = {}
    stats: dict[str, GroundingStats] = {}
    for result in client.messages.batches.results(job.batch_id):
        cid = result.custom_id
        if result.result.type != "succeeded":
            continue
        doc, source = resolve(cid)
        args = parser.tool_input(result.result.message)
        if args is None:
            continue
        recs, st = parser.parse(doc, source, args, extractor=extractor_name)
        out[cid], stats[cid] = recs, st
    return out, stats
