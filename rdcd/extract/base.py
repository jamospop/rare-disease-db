"""Extractor interface and the provenance rule that all extractors obey."""
from __future__ import annotations

from typing import Protocol

from ..corpus.jats import Document, Sentence
from ..schema import CaseRecord, Evidence, Section, SourceDoc


class Extractor(Protocol):
    name: str

    def extract(self, doc: Document, source: SourceDoc) -> list[CaseRecord]:
        """Return one CaseRecord per individual described in the document."""
        ...


def make_evidence(
    sent: Sentence, source: SourceDoc, extractor: str, *, start: int, end: int
) -> Evidence:
    """Build an Evidence, quoting the sentence only if the licence allows it.

    The offsets are recorded either way. An offset into a public document is a
    fact about that document; the sentence is the author's expression, and
    redistributing it needs permission we may not have.
    """
    return Evidence(
        source_id=source.curie,
        section=sent.section,
        start=start,
        end=end,
        quote=sent.text if source.quotes_permitted else None,
        extractor=extractor,
    )


# Headings whose content is about the literature, not about this paper's patients.
# Phenotypes named while reviewing other reports are the single largest source of
# dictionary false positives.
NON_PATIENT_HEADINGS = (
    "introduction", "background", "discussion", "conclusion", "conclusions",
    "review", "literature", "related work", "materials and methods", "methods",
    "material and methods", "statistical analysis", "funding", "acknowledg",
    "competing interests", "author contributions", "abbreviations", "references",
    "ethics", "consent", "availability of data",
)


def is_patient_section(sent: Sentence) -> bool:
    h = (sent.heading or "").strip().lower()
    if sent.section in (Section.TITLE, Section.ABSTRACT, Section.TABLE):
        return True
    if not h:
        return True
    return not any(h.startswith(x) or x in h for x in NON_PATIENT_HEADINGS)
