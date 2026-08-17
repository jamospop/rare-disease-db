"""Retraction and correction tracking.

Retracted sources are flagged, never silently deleted. A record that quietly
vanishes teaches a downstream user nothing and looks, from outside, identical to
a record we never had. A record marked `retracted` with its notice attached tells
them exactly what happened and lets them decide.

Three independent sources, because none is complete:
  * Retraction Watch (via Crossref Labs) - the authoritative registry, by DOI/PMID
  * PubMed "Retracted Publication"[pt]   - covers PMIDs without a DOI match
  * PMC OA service `retracted` attribute - already fetched during licence checks
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from ..corpus import ncbi

DATA = Path(__file__).resolve().parents[2] / "data"
RW_CSV = DATA / "ontologies" / "retractionwatch.csv"
RW_URL = "https://api.labs.crossref.org/data/retractionwatch?{email}"


@dataclass(slots=True)
class RetractionNotice:
    pmid: str | None
    doi: str | None
    reason: str | None
    retraction_date: str | None
    source: str

    def summary(self) -> str:
        bits = [f"source={self.source}"]
        if self.retraction_date:
            bits.append(f"date={self.retraction_date}")
        if self.reason:
            bits.append(f"reason={self.reason[:120]}")
        return "; ".join(bits)


def download_retraction_watch(*, force: bool = False) -> Path:
    """Fetch the Retraction Watch export. ~33 MB, cached on disk."""
    if RW_CSV.exists() and not force and RW_CSV.stat().st_size > 1_000_000:
        return RW_CSV
    body = ncbi.fetch(
        RW_URL.format(email=ncbi.EMAIL), kind="retractionwatch",
        key="retractionwatch:v1", ext="csv", force=force,
    )
    RW_CSV.parent.mkdir(parents=True, exist_ok=True)
    RW_CSV.write_text(body, encoding="utf-8")
    return RW_CSV


def load_retraction_watch() -> dict[str, RetractionNotice]:
    """Index Retraction Watch by PMID (and by DOI, lowercased)."""
    if not RW_CSV.exists():
        return {}
    idx: dict[str, RetractionNotice] = {}
    with RW_CSV.open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            # Column names have shifted historically; probe defensively.
            pmid = (row.get("OriginalPaperPubMedID") or row.get("PubMedID") or "").strip()
            doi = (row.get("OriginalPaperDOI") or row.get("DOI") or "").strip().lower()
            reason = (row.get("Reason") or "").strip(" ;+") or None
            date = (row.get("RetractionDate") or "").strip() or None
            if not (pmid or doi):
                continue
            n = RetractionNotice(pmid=pmid or None, doi=doi or None, reason=reason,
                                 retraction_date=date, source="retraction_watch")
            if pmid and pmid not in ("0", ""):
                idx[f"PMID:{pmid}"] = n
            if doi:
                idx[f"DOI:{doi}"] = n
    return idx


def pubmed_retracted(pmids: list[str]) -> set[str]:
    """Which of these PMIDs PubMed itself marks as retracted publications."""
    found: set[str] = set()
    for i in range(0, len(pmids), 400):
        chunk = [p.replace("PMID:", "") for p in pmids[i : i + 400]]
        term = '"Retracted Publication"[pt] AND (' + " OR ".join(f"{p}[uid]" for p in chunk) + ")"
        found |= {f"PMID:{x}" for x in ncbi.esearch(term, retmax=1000)}
    return found


class RetractionIndex:
    """Combined view. `check` is cheap once built."""

    def __init__(self, *, use_retraction_watch: bool = True):
        self.rw = load_retraction_watch() if use_retraction_watch else {}
        self.pubmed: set[str] = set()

    def add_pubmed(self, pmids: list[str]) -> None:
        self.pubmed |= pubmed_retracted(pmids)

    def check(self, *, pmid: str | None = None, doi: str | None = None) -> RetractionNotice | None:
        if pmid:
            key = pmid if pmid.startswith("PMID:") else f"PMID:{pmid}"
            if key in self.rw:
                return self.rw[key]
            if key in self.pubmed:
                return RetractionNotice(pmid=key.split(":", 1)[1], doi=doi, reason=None,
                                        retraction_date=None, source="pubmed_pt")
        if doi and f"DOI:{doi.lower()}" in self.rw:
            return self.rw[f"DOI:{doi.lower()}"]
        return None
