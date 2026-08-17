"""JATS XML -> sectioned plain text with stable character offsets.

Provenance is only as good as the text it points into. Every Evidence offset in
the database refers to the normalised text produced here, so this module is the
definition of "where in the paper" and must be deterministic: same XML in, same
offsets out, forever. That is why normalisation is explicit and conservative
rather than clever.

Tables matter more than usual in this domain. Multi-individual cohort papers put
the per-patient phenotype grid in a table, and the gold set is full of such
papers (median 2 cases per paper, max 462), so table text is captured as
first-class content rather than dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from lxml import etree

from ..schema import Section

# Elements whose text is never part of the narrative
DROP = {"ref-list", "back", "journal-meta", "article-id", "contrib-group",
        "aff", "author-notes", "history", "permissions", "funding-group",
        "supplementary-material", "graphic", "inline-graphic", "xref", "label"}


@dataclass(slots=True)
class Span:
    """A contiguous region of the normalised document text."""

    section: Section
    start: int
    end: int
    heading: str | None = None

    def contains(self, pos: int) -> bool:
        return self.start <= pos < self.end


@dataclass(slots=True)
class Sentence:
    text: str
    start: int
    end: int
    section: Section
    heading: str | None = None


@dataclass
class Document:
    pmcid: str | None
    pmid: str | None
    title: str | None
    text: str
    spans: list[Span] = field(default_factory=list)
    doi: str | None = None
    journal: str | None = None
    year: int | None = None
    has_body: bool = False

    def section_at(self, pos: int) -> tuple[Section, str | None]:
        for s in self.spans:
            if s.contains(pos):
                return s.section, s.heading
        return Section.UNKNOWN, None

    def sentences(self) -> list[Sentence]:
        return list(iter_sentences(self))

    def snippet(self, start: int, end: int, pad: int = 0) -> str:
        return self.text[max(0, start - pad) : min(len(self.text), end + pad)]


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------
_WS = re.compile(r"[ \t   ]+")
_NL = re.compile(r"\n{3,}")


def _norm(s: str) -> str:
    s = s.replace("‐", "-").replace("‑", "-").replace("–", "-")
    s = s.replace("—", "-").replace("−", "-")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = _WS.sub(" ", s)
    return s


def _itertext(el: etree._Element) -> Iterator[str]:
    """Text of an element, skipping citation/label noise, keeping order."""
    tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
    if tag in DROP:
        if el.tail:
            yield el.tail
        return
    if el.text:
        yield el.text
    for child in el:
        yield from _itertext(child)
    if el.tail:
        yield el.tail


def _text_of(el: etree._Element) -> str:
    return _norm("".join(_itertext(el))).strip()


def _first(root: etree._Element, xpath: str) -> etree._Element | None:
    got = root.xpath(xpath)
    return got[0] if got else None


def parse_jats(xml: str) -> Document:
    """Parse PMC efetch output. Tolerates the multi-article envelope."""
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
    root = etree.fromstring(xml.encode("utf-8", errors="replace"), parser=parser)
    art = root if etree.QName(root).localname == "article" else _first(root, ".//*[local-name()='article']")
    if art is None:
        return Document(None, None, None, "", has_body=False)

    def meta_id(kind: str) -> str | None:
        for e in art.xpath(f".//*[local-name()='article-id'][@pub-id-type='{kind}']"):
            if e.text:
                return e.text.strip()
        return None

    pmcid = meta_id("pmc")
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"

    title_el = _first(art, ".//*[local-name()='article-title']")
    title = _text_of(title_el) if title_el is not None else None
    journal_el = _first(art, ".//*[local-name()='journal-title']")
    year_el = _first(art, ".//*[local-name()='pub-date']/*[local-name()='year']")

    chunks: list[str] = []
    spans: list[Span] = []
    pos = 0

    def add(txt: str, section: Section, heading: str | None = None) -> None:
        nonlocal pos
        txt = txt.strip()
        if not txt:
            return
        chunks.append(txt)
        spans.append(Span(section, pos, pos + len(txt), heading))
        pos += len(txt) + 2  # the "\n\n" join

    if title:
        add(title, Section.TITLE)

    abstract = _first(art, ".//*[local-name()='abstract']")
    if abstract is not None:
        add(_text_of(abstract), Section.ABSTRACT)

    body = _first(art, ".//*[local-name()='body']")
    has_body = body is not None
    if body is not None:
        for sec in body.xpath(".//*[local-name()='sec']") or [body]:
            head_el = _first(sec, "./*[local-name()='title']")
            heading = _text_of(head_el) if head_el is not None else None
            for p in sec.xpath("./*[local-name()='p']"):
                add(_text_of(p), Section.BODY, heading)
        if not body.xpath(".//*[local-name()='sec']"):
            for p in body.xpath(".//*[local-name()='p']"):
                add(_text_of(p), Section.BODY, None)

    for tw in art.xpath(".//*[local-name()='table-wrap']"):
        cap_el = _first(tw, ".//*[local-name()='caption']")
        heading = _text_of(cap_el) if cap_el is not None else None
        rows = []
        for tr in tw.xpath(".//*[local-name()='tr']"):
            cells = [_text_of(td) for td in tr.xpath("./*[local-name()='td' or local-name()='th']")]
            if any(cells):
                rows.append(" | ".join(cells))
        if heading:
            add(heading, Section.TABLE, heading)
        if rows:
            add("\n".join(rows), Section.TABLE, heading)

    for fig in art.xpath(".//*[local-name()='fig']"):
        cap_el = _first(fig, ".//*[local-name()='caption']")
        if cap_el is not None:
            add(_text_of(cap_el), Section.FIGURE_CAPTION)

    text = _NL.sub("\n\n", "\n\n".join(chunks))
    return Document(
        pmcid=pmcid,
        pmid=meta_id("pmid"),
        doi=meta_id("doi"),
        title=title,
        text=text,
        spans=spans,
        journal=_text_of(journal_el) if journal_el is not None else None,
        year=int(year_el.text) if (year_el is not None and (year_el.text or "").isdigit()) else None,
        has_body=has_body,
    )


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------
# Abbreviations that must not end a sentence. Clinical text is dense with them
# and a naive split shreds exactly the sentences we need to cite.
_ABBREV = {
    "fig", "figs", "e.g", "i.e", "cf", "vs", "etc", "al", "et", "approx", "no",
    "ca", "dr", "prof", "mr", "mrs", "ms", "sr", "jr", "st", "ref", "refs",
    "min", "max", "sec", "wk", "mo", "yr", "yrs", "hr", "hrs", "mg", "ml", "kg",
    "mm", "cm", "dl", "iu", "p", "c", "g", "n", "sd", "se", "ci", "pt", "pts",
    "tab", "eq", "suppl", "chr", "exon", "nt", "aa", "pmid", "doi",
}
_SPLIT = re.compile(r"(?<=[.!?])[\)\]\"']*\s+")


def iter_sentences(doc: Document) -> Iterator[Sentence]:
    """Split into sentences, preserving exact offsets into doc.text."""
    text = doc.text
    for span in doc.spans:
        block = text[span.start : span.end]
        # Table rows are line-oriented records, not prose: split on newlines.
        if span.section == Section.TABLE:
            off = 0
            for line in block.split("\n"):
                if line.strip():
                    s = span.start + off + (len(line) - len(line.lstrip()))
                    e = span.start + off + len(line.rstrip())
                    yield Sentence(text[s:e], s, e, span.section, span.heading)
                off += len(line) + 1
            continue
        start = 0
        for m in _SPLIT.finditer(block):
            end = m.start()
            cand = block[start:end]
            last = re.split(r"[\s(]", cand.rstrip(".!?\"')]"))[-1].lower().rstrip(".")
            # Don't split after an abbreviation or a single initial.
            if last in _ABBREV or (len(last) <= 2 and last.isalpha()):
                continue
            if cand.strip():
                s = span.start + start + (len(cand) - len(cand.lstrip()))
                e = span.start + end
                yield Sentence(text[s:e], s, e, span.section, span.heading)
            start = m.end()
        tail = block[start:]
        if tail.strip():
            s = span.start + start + (len(tail) - len(tail.lstrip()))
            e = span.start + len(block.rstrip())
            yield Sentence(text[s:e], s, e, span.section, span.heading)
