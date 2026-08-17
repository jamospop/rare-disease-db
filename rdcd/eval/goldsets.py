"""Gold-standard case sets, loaded before any extraction code exists.

The plan's ordering constraint is deliberate: wire the scoring set up first, so
that extraction is developed against a fixed target instead of the target being
quietly redefined to match whatever extraction produces.

Primary gold set: GA4GH phenopacket-store (Monarch Initiative) - thousands of
case-level phenopackets curated by the people who build HPO. We never edit it.
We do filter it, and the filter is the interesting part: a case is *scoreable*
only if we can legally obtain the text it was curated from. That intersection,
not the raw case count, is the real eval-set size.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..corpus import ncbi
from ..schema import CaseRecord, SourceDoc

DATA = Path(__file__).resolve().parents[2] / "data"
STORE = DATA / "goldsets" / "phenopacket-store"


def store_version() -> str:
    versions = sorted(p.name for p in STORE.iterdir() if p.is_dir()) if STORE.exists() else []
    if not versions:
        raise FileNotFoundError(
            f"phenopacket-store not found under {STORE}. Run: make data-goldsets"
        )
    return versions[-1]


def iter_phenopackets(version: str | None = None) -> Iterator[tuple[Path, dict]]:
    v = version or store_version()
    for p in sorted((STORE / v).rglob("*.json")):
        try:
            yield p, json.loads(p.read_text())
        except json.JSONDecodeError:
            continue


def load_gold(version: str | None = None) -> list[CaseRecord]:
    """Every expert phenopacket as a CaseRecord."""
    return [CaseRecord.from_phenopacket(pkt) for _, pkt in iter_phenopackets(version)]


def group_by_pmid(cases: list[CaseRecord]) -> dict[str, list[CaseRecord]]:
    g: dict[str, list[CaseRecord]] = defaultdict(list)
    for c in cases:
        if c.source.pmid:
            g[c.source.pmid].append(c)
    return dict(g)


# ---------------------------------------------------------------------------
# Availability audit: which gold cases can we legally read the source of?
# ---------------------------------------------------------------------------
@dataclass
class Availability:
    pmid: str
    pmcid: str | None
    in_pmc: bool
    in_oa_subset: bool
    license: str | None
    retracted: bool
    quotes_permitted: bool
    n_cases: int

    @property
    def tier(self) -> str:
        """How this source may be used. Conservative by construction."""
        if not self.in_pmc:
            return "abstract_only"      # metadata + abstract + link
        if not self.in_oa_subset:
            return "abstract_only"      # in PMC but rights not granted to us
        if self.quotes_permitted:
            return "full_text_quotable"  # CC0 / CC BY / CC BY-SA
        return "full_text_facts_only"    # readable, but no redistributed prose

    def to_dict(self) -> dict:
        return {**self.__dict__, "tier": self.tier}


def audit_availability(
    pmids: list[str], *, progress: bool = True
) -> dict[str, Availability]:
    """Resolve PMID -> PMC -> OA licence for every gold source.

    Two network stages, both cached: one batched ID conversion, then one OA
    lookup per PMC article. Re-runs are free.
    """
    pmids = sorted(set(pmids))
    mapping = ncbi.pmid_to_pmcid(pmids)
    out: dict[str, Availability] = {}
    pmc_pmids = [p for p in pmids if mapping.get(p)]
    if progress:
        print(f"  {len(pmids)} PMIDs -> {len(pmc_pmids)} in PMC; checking OA licences...")
    for i, p in enumerate(pmids, 1):
        pmcid = mapping.get(p)
        if not pmcid:
            out[p] = Availability(p, None, False, False, None, False, False, 0)
            continue
        st = ncbi.oa_status(pmcid)
        out[p] = Availability(
            pmid=p,
            pmcid=pmcid,
            in_pmc=True,
            in_oa_subset=st.in_oa_subset,
            license=st.license,
            retracted=st.retracted,
            quotes_permitted=st.redistributable,
            n_cases=0,
        )
        if progress and i % 200 == 0:
            print(f"  ...{i}/{len(pmids)}")
    return out


def source_doc_for(av: Availability) -> SourceDoc:
    return SourceDoc(
        pmid=av.pmid,
        pmcid=av.pmcid,
        license=av.license,
        in_oa_subset=av.in_oa_subset,
        quotes_permitted=av.quotes_permitted,
        retracted=av.retracted,
    )
