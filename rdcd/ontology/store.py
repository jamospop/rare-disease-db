"""Unified access to HPO, MONDO, HGNC and the HPO disease annotations.

Loaded lazily and memoised, because every module downstream (grounding, QA
constraints, metrics, the diagnostic baseline) needs some slice of this and none
of them should each pay the parse cost.

The HPO annotation file (phenotype.hpoa) is doing a lot of work here. It gives
us, from one authoritative source:
  * information content per HPO term  -> semantic similarity metrics
  * disease phenotype profiles        -> the diagnostic ranking baseline
  * sex and frequency qualifiers      -> machine-checkable constraint violations
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path

from .obo import Ontology, load_cached

DATA = Path(__file__).resolve().parents[2] / "data"
ONT = DATA / "ontologies"

HPO_OBO = ONT / "hp.obo"
MONDO_OBO = ONT / "mondo.obo"
HGNC_TSV = ONT / "hgnc_complete_set.txt"
HPOA = ONT / "phenotype.hpoa"
GENES_TO_DISEASE = ONT / "genes_to_disease.txt"

# HPO frequency terms, used for obligate/excluded reasoning
HP_OBLIGATE = "HP:0040280"   # Obligate (100%)
HP_VERY_FREQUENT = "HP:0040281"
HP_EXCLUDED = "HP:0040285"   # Excluded (0%)


@dataclass(slots=True)
class GeneRecord:
    hgnc_id: str
    symbol: str
    name: str | None
    status: str
    locus_group: str | None = None
    aliases: tuple[str, ...] = ()
    prev_symbols: tuple[str, ...] = ()


@dataclass(slots=True)
class DiseaseAnnotation:
    disease_id: str
    hpo_id: str
    negated: bool
    frequency: str | None
    sex: str | None
    onset: str | None
    aspect: str | None


def _require(path: Path, make_target: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{path} missing. Run: make {make_target}")
    return path


class OntologyStore:
    """Everything ontological, loaded on first touch."""

    @cached_property
    def hpo(self) -> Ontology:
        return load_cached(_require(HPO_OBO, "data-ontologies"), id_prefix="HP:")

    @cached_property
    def mondo(self) -> Ontology:
        return load_cached(_require(MONDO_OBO, "data-ontologies"), id_prefix="MONDO:")

    # ---- genes ------------------------------------------------------------
    @cached_property
    def genes(self) -> dict[str, GeneRecord]:
        out: dict[str, GeneRecord] = {}
        with _require(HGNC_TSV, "data-ontologies").open(encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                hid = (row.get("hgnc_id") or "").strip()
                sym = (row.get("symbol") or "").strip()
                if not hid or not sym:
                    continue
                out[hid] = GeneRecord(
                    hgnc_id=hid,
                    symbol=sym,
                    name=(row.get("name") or None),
                    status=(row.get("status") or "").strip(),
                    locus_group=(row.get("locus_group") or None),
                    aliases=tuple(
                        s.strip().strip('"') for s in (row.get("alias_symbol") or "").split("|") if s.strip()
                    ),
                    prev_symbols=tuple(
                        s.strip().strip('"') for s in (row.get("prev_symbol") or "").split("|") if s.strip()
                    ),
                )
        return out

    @cached_property
    def _symbol_index(self) -> dict[str, str]:
        """Symbol/alias/previous-symbol (upper) -> HGNC id.

        Approved symbols win over aliases: case reports written years apart use
        different symbols for the same gene, and an alias collision that
        overwrote an approved symbol would silently mis-assign a gene.
        """
        approved: dict[str, str] = {}
        secondary: dict[str, str] = {}
        for g in self.genes.values():
            approved[g.symbol.upper()] = g.hgnc_id
        for g in self.genes.values():
            for s in (*g.aliases, *g.prev_symbols):
                k = s.upper()
                if k not in approved:
                    secondary.setdefault(k, g.hgnc_id)
        return {**secondary, **approved}

    def gene_id(self, symbol: str) -> str | None:
        return self._symbol_index.get((symbol or "").strip().upper())

    def gene_symbol(self, hgnc_id: str) -> str | None:
        g = self.genes.get((hgnc_id or "").strip())
        return g.symbol if g else None

    def canonical_gene_symbol(self, symbol: str) -> str | None:
        """Map any historical symbol to today's approved one."""
        hid = self.gene_id(symbol)
        return self.gene_symbol(hid) if hid else None

    # ---- diseases ---------------------------------------------------------
    @cached_property
    def _xref_to_mondo(self) -> dict[str, list[str]]:
        eq = self.mondo.xref_index(("OMIM:", "Orphanet:", "ORPHA:", "DECIPHER:"),
                                   predicate="MONDO:equivalentTo")
        allx = self.mondo.xref_index(("OMIM:", "Orphanet:", "ORPHA:", "DECIPHER:"))
        # equivalentTo is authoritative; fall back to any xref so we lose nothing
        merged = dict(allx)
        merged.update(eq)
        return merged

    def normalize_disease(self, disease_id: str) -> str | None:
        """Any disease CURIE -> canonical MONDO id.

        Gold phenopackets use OMIM ids, HPOA uses OMIM and Orphanet, MONDO is the
        common denominator. Scoring diagnoses without this normalisation would
        count correct answers as wrong purely because of vocabulary.
        """
        if not disease_id:
            return None
        d = disease_id.strip()
        if d.startswith("MONDO:"):
            return self.mondo.normalize(d)
        if d.upper().startswith("ORPHA:"):
            d = "Orphanet:" + d.split(":", 1)[1]
        ids = self._xref_to_mondo.get(d)
        if ids:
            return ids[0]
        return None

    def disease_label(self, disease_id: str) -> str | None:
        m = self.normalize_disease(disease_id)
        return self.mondo.label(m) if m else None

    # ---- HPO disease annotations -----------------------------------------
    @cached_property
    def annotations(self) -> list[DiseaseAnnotation]:
        rows: list[DiseaseAnnotation] = []
        with _require(HPOA, "data-ontologies").open(encoding="utf-8", errors="replace") as fh:
            lines = (ln for ln in fh if not ln.startswith("#"))
            for row in csv.DictReader(lines, delimiter="\t"):
                hid = (row.get("hpo_id") or "").strip()
                did = (row.get("database_id") or "").strip()
                if not hid or not did:
                    continue
                rows.append(
                    DiseaseAnnotation(
                        disease_id=did,
                        hpo_id=hid,
                        negated=(row.get("qualifier") or "").strip().upper() == "NOT",
                        frequency=(row.get("frequency") or None),
                        sex=(row.get("sex") or None),
                        onset=(row.get("onset") or None),
                        aspect=(row.get("aspect") or None),
                    )
                )
        return rows

    @cached_property
    def disease_profiles(self) -> dict[str, set[str]]:
        """Canonical MONDO id -> set of positively annotated HPO terms."""
        prof: dict[str, set[str]] = defaultdict(set)
        for a in self.annotations:
            if a.negated or a.aspect not in (None, "P"):
                continue
            m = self.normalize_disease(a.disease_id)
            h = self.hpo.normalize(a.hpo_id)
            if m and h:
                prof[m].add(h)
        return dict(prof)

    @cached_property
    def disease_excluded_profiles(self) -> dict[str, set[str]]:
        """Canonical MONDO id -> HPO terms annotated NOT / Excluded."""
        prof: dict[str, set[str]] = defaultdict(set)
        for a in self.annotations:
            if not (a.negated or (a.frequency or "") == HP_EXCLUDED):
                continue
            m = self.normalize_disease(a.disease_id)
            h = self.hpo.normalize(a.hpo_id)
            if m and h:
                prof[m].add(h)
        return dict(prof)

    @cached_property
    def disease_sex_constraint(self) -> dict[str, str]:
        """Canonical MONDO id -> 'MALE'/'FEMALE' when every annotation is sex-limited."""
        seen: dict[str, set[str]] = defaultdict(set)
        for a in self.annotations:
            m = self.normalize_disease(a.disease_id)
            if not m:
                continue
            seen[m].add((a.sex or "").strip().upper() or "")
        out: dict[str, str] = {}
        for m, sexes in seen.items():
            named = {s for s in sexes if s in ("MALE", "FEMALE")}
            if len(named) == 1 and not (sexes - named):
                out[m] = next(iter(named))
        return out

    @cached_property
    def obligate_phenotypes(self) -> dict[str, set[str]]:
        """Canonical MONDO id -> HPO terms annotated as obligate (100%)."""
        prof: dict[str, set[str]] = defaultdict(set)
        for a in self.annotations:
            f = (a.frequency or "").strip()
            if a.negated:
                continue
            if f == HP_OBLIGATE or f in ("1/1", "100%"):
                m = self.normalize_disease(a.disease_id)
                h = self.hpo.normalize(a.hpo_id)
                if m and h:
                    prof[m].add(h)
        return dict(prof)

    # ---- gene <-> disease associations -----------------------------------
    @cached_property
    def _entrez_to_hgnc(self) -> dict[str, str]:
        out: dict[str, str] = {}
        with HGNC_TSV.open(encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                ez = (row.get("entrez_id") or "").strip()
                hid = (row.get("hgnc_id") or "").strip()
                if ez and hid:
                    out[ez] = hid
        return out

    @cached_property
    def gene_disease(self) -> dict[str, set[str]]:
        """HGNC id -> set of canonical MONDO ids it is known to cause."""
        out: dict[str, set[str]] = defaultdict(set)
        if not GENES_TO_DISEASE.exists():
            return {}
        with GENES_TO_DISEASE.open(encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                ncbi_id = (row.get("ncbi_gene_id") or "").replace("NCBIGene:", "").strip()
                hid = self._entrez_to_hgnc.get(ncbi_id) or self.gene_id(row.get("gene_symbol") or "")
                m = self.normalize_disease((row.get("disease_id") or "").strip())
                if hid and m:
                    out[hid].add(m)
        return dict(out)

    @cached_property
    def disease_genes(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for g, ds in self.gene_disease.items():
            for d in ds:
                out[d].add(g)
        return dict(out)

    @cached_property
    def disease_inheritance(self) -> dict[str, set[str]]:
        """Canonical MONDO id -> HPO inheritance-mode terms (aspect=I)."""
        out: dict[str, set[str]] = defaultdict(set)
        for a in self.annotations:
            if a.aspect != "I" or a.negated:
                continue
            m = self.normalize_disease(a.disease_id)
            h = self.hpo.normalize(a.hpo_id)
            if m and h:
                out[m].add(h)
        return dict(out)

    # ---- information content ---------------------------------------------
    @cached_property
    def _term_disease_counts(self) -> dict[str, int]:
        """How many diseases each HPO term (or any descendant) annotates.

        Counts propagate up the DAG: annotating a disease with 'Focal seizure'
        also counts toward 'Seizure'. Without propagation, general terms would
        look rarer than their own children and the IC would be nonsense.
        """
        counts: dict[str, set[str]] = defaultdict(set)
        for a in self.annotations:
            if a.negated:
                continue
            h = self.hpo.normalize(a.hpo_id)
            if not h:
                continue
            for anc in self.hpo.ancestors(h):
                counts[anc].add(a.disease_id)
        return {k: len(v) for k, v in counts.items()}

    @cached_property
    def _n_annotated_diseases(self) -> int:
        return len({a.disease_id for a in self.annotations if not a.negated})

    @lru_cache(maxsize=100_000)
    def information_content(self, hpo_id: str) -> float:
        """-log(p(term)). Higher = more specific = more diagnostically informative."""
        h = self.hpo.normalize(hpo_id)
        if not h:
            return 0.0
        n = self._term_disease_counts.get(h, 0)
        total = self._n_annotated_diseases
        if not n or not total:
            # Unannotated terms are maximally specific by this measure; cap them
            # at the rarest observed value instead of infinity.
            return math.log(total) if total else 0.0
        return -math.log(n / total)

    def most_informative_common_ancestor(self, a: str, b: str) -> tuple[str | None, float]:
        anc = self.hpo.ancestors(a) & self.hpo.ancestors(b)
        best, best_ic = None, 0.0
        for t in anc:
            ic = self.information_content(t)
            if ic >= best_ic:
                best, best_ic = t, ic
        return best, best_ic


STORE = OntologyStore()
