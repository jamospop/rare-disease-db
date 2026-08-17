"""Verify that each cited span actually supports the assertion it justifies.

This is the check that makes the provenance rule more than a promise. For every
assertion, we re-read the exact characters the extractor pointed at and ask: does
this span ground to the term claimed, with the claimed polarity?

Deliberately mechanical. It needs no medical knowledge, which is the entire
argument for the provenance-or-null design: a non-clinician auditor (or this
function) can check "does the sentence say this?" even when "is this diagnosis
correct?" is out of reach. The measured failure rate is the hallucination rate,
and it is published in docs/ERROR_LEDGER.md.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..corpus.jats import Document
from ..ontology.grounding import Grounder
from ..ontology.store import OntologyStore
from ..schema import CaseRecord, PhenotypeAssertion

# Verdicts
OK = "supported"
NO_EVIDENCE = "no_evidence"
BAD_OFFSET = "offset_out_of_range"
EMPTY_SPAN = "empty_span"
TERM_ABSENT = "term_not_in_span"
POLARITY_MISMATCH = "polarity_mismatch"


@dataclass(slots=True)
class SpanVerdict:
    assertion: str
    verdict: str
    span_text: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == OK


class ProvenanceVerifier:
    def __init__(self, store: OntologyStore, *, sentence_pad: int = 240):
        self.store = store
        self.grounder = Grounder(store)
        self.pad = sentence_pad

    def verify_phenotype(
        self, doc: Document, p: PhenotypeAssertion
    ) -> list[SpanVerdict]:
        name = f"phenotype:{p.term.id}"
        if not p.evidence:
            return [SpanVerdict(name, NO_EVIDENCE)]
        out: list[SpanVerdict] = []
        for ev in p.evidence:
            if ev.start is None or ev.end is None:
                out.append(SpanVerdict(name, NO_EVIDENCE, detail="evidence has no offsets"))
                continue
            if ev.end > len(doc.text) or ev.start < 0:
                out.append(SpanVerdict(name, BAD_OFFSET,
                                       detail=f"span {ev.start}-{ev.end} vs doc len {len(doc.text)}"))
                continue
            span = doc.text[ev.start : ev.end]
            if not span.strip():
                out.append(SpanVerdict(name, EMPTY_SPAN))
                continue
            # Does the span ground to the claimed term, or a close relative?
            claimed = self.store.hpo.normalize(p.term.id)
            found = {
                self.store.hpo.normalize(m.term_id)
                for m in self.grounder.find(span)
            }
            found.discard(None)
            related = False
            if claimed:
                for f in found:
                    if f == claimed or claimed in self.store.hpo.ancestors(f) \
                       or f in self.store.hpo.ancestors(claimed):
                        related = True
                        break
            if not related:
                out.append(SpanVerdict(name, TERM_ABSENT, span_text=span[:120],
                                       detail=f"span grounds to {sorted(x for x in found if x)[:3]}"))
                continue
            # Polarity: re-read the surrounding sentence, not just the term span.
            ctx_start = max(0, ev.start - self.pad)
            ctx = doc.text[ctx_start : min(len(doc.text), ev.end + self.pad)]
            rel_start = ev.start - ctx_start
            cue = self.grounder._negation_cue(ctx.lower(), rel_start, rel_start + len(span))
            if bool(cue) != bool(p.excluded):
                out.append(SpanVerdict(
                    name, POLARITY_MISMATCH, span_text=span[:120],
                    detail=f"excluded={p.excluded} but context cue={cue!r}"))
                continue
            out.append(SpanVerdict(name, OK, span_text=span[:120]))
        return out

    def verify(self, doc: Document, rec: CaseRecord) -> list[SpanVerdict]:
        out: list[SpanVerdict] = []
        for p in rec.phenotypes:
            out.extend(self.verify_phenotype(doc, p))
        for v in rec.variants:
            name = f"variant:{v.gene.label if v.gene else v.hgvs_c}"
            if not v.evidence:
                out.append(SpanVerdict(name, NO_EVIDENCE))
                continue
            ev = v.evidence[0]
            if ev.start is None or ev.end is None or ev.end > len(doc.text):
                out.append(SpanVerdict(name, BAD_OFFSET))
                continue
            span = doc.text[ev.start : ev.end]
            # Resolve through HGNC: papers cite historical symbols and aliases,
            # so "FOG2" in the span does support the approved symbol ZFPM2. A
            # literal string comparison here would report the extractor as
            # hallucinating when it had correctly normalised a synonym.
            supported = False
            if v.gene:
                claimed_id = v.gene.id
                span_id = self.store.gene_id(span.strip())
                supported = span_id == claimed_id or (v.gene.label or "") in span
            if not supported and v.hgvs_c:
                supported = v.hgvs_c in span
            out.append(SpanVerdict(name, OK if supported else TERM_ABSENT,
                                   span_text=span[:80],
                                   detail=None if supported else "span does not resolve to claimed gene"))
        return out

    def rates(self, verdicts: list[SpanVerdict]) -> dict:
        c = Counter(v.verdict for v in verdicts)
        n = len(verdicts) or 1
        return {
            "n_assertions_checked": len(verdicts),
            "supported": c[OK],
            "support_rate": round(c[OK] / n, 4),
            "unsupported_rate": round(1 - c[OK] / n, 4),
            "by_verdict": dict(c.most_common()),
        }
