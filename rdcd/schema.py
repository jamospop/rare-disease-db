"""Schema v1 for the Open Rare-Disease Case Database.

Two hard commitments encoded here:

1. **Phenopacket-compatible.** Every record exports to a GA4GH Phenopacket v2
   without loss of the fields Phenopackets models. This is why the eval set can
   be expert phenopackets: gold and prediction live in the same shape.

2. **Provenance-or-null.** Every asserted fact carries at least one Evidence
   pointing at the sentence it came from. A fact with no evidence is not a
   low-confidence fact, it is not a fact: strict validation drops it. This is
   what turns medical verification into reading comprehension - an auditor
   without clinical training can check "does this sentence say this?" even when
   they could not judge "is this diagnosis right?".

Evidence stores character offsets always, and the quoted sentence only when the
source licence permits redistributing expression (see docs/LICENSING.md). Offsets
plus a PMID are facts about a document; the sentence itself is someone's prose.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
class Section(str, Enum):
    TITLE = "title"
    ABSTRACT = "abstract"
    BODY = "body"
    TABLE = "table"
    FIGURE_CAPTION = "figure_caption"
    SUPPLEMENT = "supplement"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    """A pointer into a source document supporting exactly one assertion."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(description="Curie of the source, e.g. PMID:36996813")
    section: Section = Section.UNKNOWN
    start: int | None = Field(default=None, ge=0, description="Char offset into normalised text")
    end: int | None = Field(default=None, ge=0)
    quote: str | None = Field(
        default=None,
        description="Verbatim sentence. Populated ONLY when the source licence "
        "permits redistribution of expression; otherwise None and the reader "
        "follows source_id + offsets.",
    )
    extractor: str | None = Field(default=None, description="Which extractor produced this")

    @model_validator(mode="after")
    def _span_sane(self) -> "Evidence":
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError(f"evidence end {self.end} precedes start {self.start}")
        return self

    @property
    def locator(self) -> str:
        if self.start is None:
            return f"{self.source_id}#{self.section.value}"
        return f"{self.source_id}#{self.section.value}:{self.start}-{self.end}"


class Asserted(BaseModel):
    """Base for anything that must justify itself."""

    model_config = ConfigDict(extra="forbid")

    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def has_provenance(self) -> bool:
        return len(self.evidence) > 0


# ---------------------------------------------------------------------------
# Ontology terms and time
# ---------------------------------------------------------------------------
class OntologyClass(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="CURIE, e.g. HP:0001250 / MONDO:0007947 / HGNC:17474")
    label: str | None = None

    @field_validator("id")
    @classmethod
    def _curie(cls, v: str) -> str:
        v = v.strip()
        if ":" not in v:
            raise ValueError(f"not a CURIE: {v!r}")
        return v

    @property
    def prefix(self) -> str:
        return self.id.split(":", 1)[0]


class TimeElement(BaseModel):
    """Mirrors the Phenopacket TimeElement one-of, in the forms case reports use."""

    model_config = ConfigDict(extra="forbid")

    iso8601duration: str | None = Field(default=None, description="Age, e.g. P32Y6M")
    gestational_weeks: int | None = Field(default=None, ge=0, le=50)
    gestational_days: int | None = Field(default=None, ge=0, le=6)
    onset_class: OntologyClass | None = Field(
        default=None, description="HPO onset term, e.g. HP:0003577 Congenital onset"
    )
    age_range_low: str | None = None
    age_range_high: str | None = None

    def to_phenopacket(self) -> dict[str, Any] | None:
        if self.iso8601duration:
            return {"age": {"iso8601duration": self.iso8601duration}}
        if self.gestational_weeks is not None:
            g: dict[str, Any] = {"weeks": self.gestational_weeks}
            if self.gestational_days is not None:
                g["days"] = self.gestational_days
            return {"gestationalAge": g}
        if self.onset_class:
            return {"ontologyClass": self.onset_class.model_dump(exclude_none=True)}
        if self.age_range_low and self.age_range_high:
            return {
                "ageRange": {
                    "start": {"age": {"iso8601duration": self.age_range_low}},
                    "end": {"age": {"iso8601duration": self.age_range_high}},
                }
            }
        return None

    @classmethod
    def from_phenopacket(cls, d: dict[str, Any] | None) -> "TimeElement | None":
        if not d:
            return None
        if "age" in d:
            return cls(iso8601duration=d["age"].get("iso8601duration"))
        if "gestationalAge" in d:
            g = d["gestationalAge"]
            return cls(gestational_weeks=g.get("weeks"), gestational_days=g.get("days"))
        if "ontologyClass" in d:
            return cls(onset_class=OntologyClass(**d["ontologyClass"]))
        if "ageRange" in d:
            r = d["ageRange"]
            return cls(
                age_range_low=r.get("start", {}).get("age", {}).get("iso8601duration"),
                age_range_high=r.get("end", {}).get("age", {}).get("iso8601duration"),
            )
        return None


# ---------------------------------------------------------------------------
# Clinical content
# ---------------------------------------------------------------------------
Sex = Literal["MALE", "FEMALE", "OTHER_SEX", "UNKNOWN_SEX"]
VitalStatus = Literal["ALIVE", "DECEASED", "UNKNOWN_STATUS"]


class PhenotypeAssertion(Asserted):
    """One phenotype, present or explicitly absent.

    `excluded=True` means the source states the feature was looked for and not
    found. 59% of features in phenopacket-store are excluded, so a pipeline that
    cannot represent negation cannot be scored against expert data at all.
    """

    term: OntologyClass
    excluded: bool = False
    onset: TimeElement | None = None
    severity: OntologyClass | None = None
    negation_cue: str | None = Field(
        default=None, description="The word that carried the negation, for audit"
    )
    grounded_from: str | None = Field(
        default=None,
        description=(
            "The text that was grounded to produce `term`. Usually the evidence quote "
            "itself; for an extractor that supplies a normalised label, the label. "
            "Recorded so verification can re-check the SAME derivation instead of "
            "guessing at it: clinical prose states absence as 'X was normal', which "
            "contains no ontology term, so re-grounding the raw span would report a "
            "correctly-derived assertion as unsupported."
        ),
    )

    def to_phenopacket(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.term.model_dump(exclude_none=True)}
        if self.excluded:
            d["excluded"] = True
        if self.onset and (t := self.onset.to_phenopacket()):
            d["onset"] = t
        if self.severity:
            d["severity"] = self.severity.model_dump(exclude_none=True)
        return d


class VariantAssertion(Asserted):
    model_config = ConfigDict(extra="forbid")

    gene: OntologyClass | None = Field(default=None, description="HGNC id + symbol")
    hgvs_c: str | None = None
    hgvs_p: str | None = None
    hgvs_g: str | None = None
    allelic_state: OntologyClass | None = Field(default=None, description="GENO term")
    acmg: (
        Literal[
            "BENIGN",
            "LIKELY_BENIGN",
            "UNCERTAIN_SIGNIFICANCE",
            "LIKELY_PATHOGENIC",
            "PATHOGENIC",
            "NOT_PROVIDED",
        ]
        | None
    ) = None
    evidence: list[Evidence] = Field(default_factory=list)


class DiagnosisAssertion(Asserted):
    disease: OntologyClass
    status: Literal["SOLVED", "UNSOLVED", "IN_PROGRESS", "UNKNOWN_PROGRESS"] = "UNKNOWN_PROGRESS"
    stated_in_abstract: bool = Field(
        default=False,
        description="True when the diagnosis appears in title/abstract. These "
        "cases are the distant-supervision validation set (docs/QA.md).",
    )


class Subject(Asserted):
    id: str = "index"
    sex: Sex = "UNKNOWN_SEX"
    age_at_last_encounter: TimeElement | None = None
    vital_status: VitalStatus = "UNKNOWN_STATUS"

    def to_phenopacket(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "sex": self.sex}
        if self.age_at_last_encounter and (t := self.age_at_last_encounter.to_phenopacket()):
            d["timeAtLastEncounter"] = t
        if self.vital_status != "UNKNOWN_STATUS":
            d["vitalStatus"] = {"status": self.vital_status}
        return d


class SourceDoc(BaseModel):
    """Provenance and licence of the document a case was read from."""

    model_config = ConfigDict(extra="forbid")

    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    title: str | None = None
    journal: str | None = None
    year: int | None = None
    license: str | None = None
    in_oa_subset: bool = False
    quotes_permitted: bool = Field(
        default=False, description="Whether Evidence.quote may be populated for this source"
    )
    retracted: bool = False
    retraction_notice: str | None = None

    @property
    def curie(self) -> str:
        if self.pmid:
            return f"PMID:{self.pmid}"
        if self.pmcid:
            return self.pmcid
        return f"DOI:{self.doi}" if self.doi else "UNKNOWN:0"


class CaseRecord(BaseModel):
    """One individual, extracted from one document."""

    model_config = ConfigDict(extra="forbid")

    id: str
    schema_version: str = SCHEMA_VERSION
    source: SourceDoc
    subject: Subject = Field(default_factory=Subject)
    phenotypes: list[PhenotypeAssertion] = Field(default_factory=list)
    diagnoses: list[DiagnosisAssertion] = Field(default_factory=list)
    variants: list[VariantAssertion] = Field(default_factory=list)

    # QA annotations, written by rdcd.qa - never by an extractor
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    qa_flags: list[str] = Field(default_factory=list)
    extractors: list[str] = Field(default_factory=list)

    # ---- convenience views used by the eval harness -----------------------
    @property
    def observed_hpo(self) -> set[str]:
        return {p.term.id for p in self.phenotypes if not p.excluded}

    @property
    def excluded_hpo(self) -> set[str]:
        return {p.term.id for p in self.phenotypes if p.excluded}

    @property
    def gene_symbols(self) -> set[str]:
        return {v.gene.label for v in self.variants if v.gene and v.gene.label}

    @property
    def gene_ids(self) -> set[str]:
        return {v.gene.id for v in self.variants if v.gene}

    @property
    def disease_ids(self) -> set[str]:
        return {d.disease.id for d in self.diagnoses}

    # ---- provenance-or-null ----------------------------------------------
    def unprovenanced(self) -> list[str]:
        """Field paths asserting something without evidence."""
        bad: list[str] = []
        for i, p in enumerate(self.phenotypes):
            if not p.has_provenance:
                bad.append(f"phenotypes[{i}]={p.term.id}")
        for i, d in enumerate(self.diagnoses):
            if not d.has_provenance:
                bad.append(f"diagnoses[{i}]={d.disease.id}")
        for i, v in enumerate(self.variants):
            if not v.has_provenance:
                bad.append(f"variants[{i}]={v.hgvs_c or v.gene}")
        return bad

    def enforce_provenance(self) -> tuple["CaseRecord", list[str]]:
        """Drop every unprovenanced assertion. Returns (clean record, dropped)."""
        dropped = self.unprovenanced()
        clean = self.model_copy(
            update={
                "phenotypes": [p for p in self.phenotypes if p.has_provenance],
                "diagnoses": [d for d in self.diagnoses if d.has_provenance],
                "variants": [v for v in self.variants if v.has_provenance],
            }
        )
        return clean, dropped

    # ---- export ----------------------------------------------------------
    def to_phenopacket(self) -> dict[str, Any]:
        """GA4GH Phenopacket v2 (JSON form). Round-trips with from_phenopacket."""
        interpretations = []
        for d in self.diagnoses:
            gi = []
            for v in self.variants:
                vd: dict[str, Any] = {"id": v.hgvs_c or v.hgvs_g or "variant"}
                if v.gene:
                    vd["geneContext"] = {"valueId": v.gene.id, "symbol": v.gene.label}
                exprs = [
                    {"syntax": s, "value": val}
                    for s, val in (("hgvs.c", v.hgvs_c), ("hgvs.g", v.hgvs_g), ("hgvs.p", v.hgvs_p))
                    if val
                ]
                if exprs:
                    vd["expressions"] = exprs
                if v.allelic_state:
                    vd["allelicState"] = v.allelic_state.model_dump(exclude_none=True)
                gi.append(
                    {
                        "subjectOrBiosampleId": self.subject.id,
                        "interpretationStatus": "CAUSATIVE",
                        "variantInterpretation": {
                            "acmgPathogenicityClassification": v.acmg or "NOT_PROVIDED",
                            "variationDescriptor": vd,
                        },
                    }
                )
            interpretations.append(
                {
                    "id": f"{self.id}-interp",
                    "progressStatus": d.status,
                    "diagnosis": {
                        "disease": d.disease.model_dump(exclude_none=True),
                        **({"genomicInterpretations": gi} if gi else {}),
                    },
                }
            )
        pkt: dict[str, Any] = {
            "id": self.id,
            "subject": self.subject.to_phenopacket(),
            "phenotypicFeatures": [p.to_phenopacket() for p in self.phenotypes],
            "metaData": {
                "createdBy": "rdcd",
                "phenopacketSchemaVersion": "2.0",
                "externalReferences": (
                    [
                        {
                            "id": self.source.curie,
                            **({"reference": self.source.doi} if self.source.doi else {}),
                            **({"description": self.source.title} if self.source.title else {}),
                        }
                    ]
                    if self.source.curie != "UNKNOWN:0"
                    else []
                ),
            },
        }
        if interpretations:
            pkt["interpretations"] = interpretations
        if self.diagnoses:
            pkt["diseases"] = [
                d.disease.model_dump(exclude_none=True) for d in self.diagnoses
            ]
        return pkt

    @classmethod
    def from_phenopacket(
        cls, pkt: dict[str, Any], *, source_hint: SourceDoc | None = None, extractor: str = "gold"
    ) -> "CaseRecord":
        """Read an expert phenopacket into a CaseRecord.

        Gold phenopackets carry no per-field evidence, so each assertion gets a
        document-level Evidence: true (the paper does support it) without
        claiming a sentence offset we do not know.
        """
        ext_refs = (pkt.get("metaData") or {}).get("externalReferences") or []
        pmid = None
        for er in ext_refs:
            rid = str(er.get("id", ""))
            if rid.upper().startswith("PMID"):
                pmid = rid.split(":", 1)[1] if ":" in rid else rid[4:]
                break
        src = source_hint or SourceDoc(
            pmid=pmid,
            title=(ext_refs[0].get("description") if ext_refs else None),
        )
        doc_ev = [Evidence(source_id=src.curie, section=Section.UNKNOWN, extractor=extractor)]

        phenos = []
        for pf in pkt.get("phenotypicFeatures") or []:
            t = pf.get("type") or {}
            if not t.get("id"):
                continue
            phenos.append(
                PhenotypeAssertion(
                    term=OntologyClass(id=t["id"], label=t.get("label")),
                    excluded=bool(pf.get("excluded", False)),
                    onset=TimeElement.from_phenopacket(pf.get("onset")),
                    severity=(
                        OntologyClass(**pf["severity"]) if pf.get("severity", {}).get("id") else None
                    ),
                    evidence=list(doc_ev),
                )
            )

        diagnoses, variants = [], []
        for it in pkt.get("interpretations") or []:
            dx = it.get("diagnosis") or {}
            dis = dx.get("disease") or {}
            if dis.get("id"):
                diagnoses.append(
                    DiagnosisAssertion(
                        disease=OntologyClass(id=dis["id"], label=dis.get("label")),
                        status=it.get("progressStatus", "UNKNOWN_PROGRESS"),
                        evidence=list(doc_ev),
                    )
                )
            for g in dx.get("genomicInterpretations") or []:
                vd = (g.get("variantInterpretation") or {}).get("variationDescriptor") or {}
                exprs = {e.get("syntax"): e.get("value") for e in vd.get("expressions") or []}
                gc = vd.get("geneContext") or {}
                variants.append(
                    VariantAssertion(
                        gene=(
                            OntologyClass(id=gc["valueId"], label=gc.get("symbol"))
                            if gc.get("valueId")
                            else None
                        ),
                        hgvs_c=exprs.get("hgvs.c"),
                        hgvs_p=exprs.get("hgvs.p"),
                        hgvs_g=exprs.get("hgvs.g"),
                        allelic_state=(
                            OntologyClass(**vd["allelicState"])
                            if (vd.get("allelicState") or {}).get("id")
                            else None
                        ),
                        acmg=(g.get("variantInterpretation") or {}).get(
                            "acmgPathogenicityClassification"
                        ),
                        evidence=list(doc_ev),
                    )
                )
        if not diagnoses:
            for dis in pkt.get("diseases") or []:
                if dis.get("id"):
                    diagnoses.append(
                        DiagnosisAssertion(
                            disease=OntologyClass(id=dis["id"], label=dis.get("label")),
                            evidence=list(doc_ev),
                        )
                    )

        subj = pkt.get("subject") or {}
        return cls(
            id=pkt.get("id") or hashlib.sha1(json.dumps(pkt, sort_keys=True).encode()).hexdigest()[:12],
            source=src,
            subject=Subject(
                id=subj.get("id") or "index",
                sex=subj.get("sex") or "UNKNOWN_SEX",
                age_at_last_encounter=TimeElement.from_phenopacket(subj.get("timeAtLastEncounter")),
                vital_status=(subj.get("vitalStatus") or {}).get("status") or "UNKNOWN_STATUS",
                evidence=list(doc_ev),
            ),
            phenotypes=phenos,
            diagnoses=diagnoses,
            variants=variants,
            extractors=[extractor],
        )
