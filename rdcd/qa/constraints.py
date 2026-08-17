"""Machine-checkable impossibilities.

These checks need no clinician and no LLM. They catch a specific, valuable class
of error: records that are internally incoherent or that contradict curated
biology. A flagged record is never silently deleted - it is published with its
flags, because a wrong record you can see is worth more than a missing record you
cannot.

Severity is honest about what is truly impossible versus merely improbable:
  ERROR  - cannot be true (a term both present and absent; a child present while
           its parent is absent; onset after the last encounter)
  WARN   - contradicts curated knowledge, but real exceptions exist (a gene not
           previously linked to the diagnosis is how new associations get found)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..ontology.store import OntologyStore
from ..schema import CaseRecord

SEV_ERROR = "ERROR"
SEV_WARN = "WARN"

HP_Y_LINKED = "HP:0001450"
HP_X_LINKED_RECESSIVE = "HP:0001419"


@dataclass(slots=True)
class Violation:
    code: str
    severity: str
    detail: str
    terms: tuple[str, ...] = ()

    def as_flag(self) -> str:
        return f"{self.severity.lower()}:{self.code}"


# Weeks are included: gestational and neonatal ages are written PnW, and a
# parser that silently returned None for those would skip the check entirely.
_ISO = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$")


def _iso_to_days(iso: str | None) -> float | None:
    if not iso:
        return None
    m = _ISO.match(iso.strip())
    if not m:
        return None
    y, mo, w, d = (int(x) if x else 0 for x in m.groups())
    if not any((y, mo, w, d)) and iso.strip() != "P0D":
        return None
    return y * 365.25 + mo * 30.44 + w * 7 + d


def check(store: OntologyStore, rec: CaseRecord) -> list[Violation]:
    out: list[Violation] = []
    hpo = store.hpo

    observed = {t for t in (hpo.normalize(x) for x in rec.observed_hpo) if t}
    excluded = {t for t in (hpo.normalize(x) for x in rec.excluded_hpo) if t}

    # 1. Same term asserted both present and absent.
    for t in observed & excluded:
        out.append(
            Violation("polarity_contradiction", SEV_ERROR,
                      f"{t} ({hpo.label(t)}) asserted both observed and excluded", (t,))
        )

    # 2. A specific finding present while a more general one is absent.
    #    If focal seizure is present, seizure cannot be absent.
    for child in observed:
        for anc in hpo.ancestors(child, include_self=False):
            if anc in excluded:
                out.append(
                    Violation(
                        "dag_polarity_contradiction", SEV_ERROR,
                        f"{child} ({hpo.label(child)}) observed but ancestor "
                        f"{anc} ({hpo.label(anc)}) excluded",
                        (child, anc),
                    )
                )

    # 3. Unknown or obsolete ontology terms.
    for p in rec.phenotypes:
        norm = hpo.normalize(p.term.id)
        if norm is None:
            out.append(Violation("unknown_hpo_term", SEV_WARN,
                                 f"{p.term.id} not present in current HPO", (p.term.id,)))
        elif norm != p.term.id:
            out.append(Violation("outdated_hpo_term", SEV_WARN,
                                 f"{p.term.id} is an alias/obsolete form of {norm}",
                                 (p.term.id, norm)))

    # 4. Onset later than the last encounter.
    enc = _iso_to_days(
        rec.subject.age_at_last_encounter.iso8601duration
        if rec.subject.age_at_last_encounter else None
    )
    if enc is not None:
        for p in rec.phenotypes:
            on = _iso_to_days(p.onset.iso8601duration if p.onset else None)
            if on is not None and on > enc + 1:
                out.append(
                    Violation("onset_after_last_encounter", SEV_ERROR,
                              f"{p.term.id} onset {p.onset.iso8601duration} exceeds age at "
                              f"last encounter {rec.subject.age_at_last_encounter.iso8601duration}",
                              (p.term.id,))
                )

    # 5. Diagnosis-level coherence.
    for d in rec.diagnoses:
        mid = store.normalize_disease(d.disease.id)
        if not mid:
            out.append(Violation("unmappable_disease", SEV_WARN,
                                 f"{d.disease.id} does not map to MONDO", (d.disease.id,)))
            continue

        # An obligate feature of the disease reported as explicitly absent.
        for ob in store.obligate_phenotypes.get(mid, set()):
            if ob in excluded:
                out.append(
                    Violation("obligate_phenotype_excluded", SEV_WARN,
                              f"{ob} ({hpo.label(ob)}) is obligate for {mid} "
                              f"({store.mondo.label(mid)}) but recorded as absent", (mid, ob))
                )

        # Y-linked disease in a female subject is not possible.
        inh = store.disease_inheritance.get(mid, set())
        if HP_Y_LINKED in inh and rec.subject.sex == "FEMALE":
            out.append(
                Violation("sex_inheritance_conflict", SEV_ERROR,
                          f"{mid} is Y-linked but subject sex is FEMALE", (mid,))
            )

        # Gene not a known cause of this disease.
        known = store.disease_genes.get(mid, set())
        if known:
            for g in rec.gene_ids:
                if g and g not in known:
                    out.append(
                        Violation("gene_disease_mismatch", SEV_WARN,
                                  f"{store.gene_symbol(g) or g} is not a known cause of "
                                  f"{mid} ({store.mondo.label(mid)})", (mid, g))
                    )

    return out


def check_many(store: OntologyStore, recs: Iterable[CaseRecord]) -> dict[str, int]:
    from collections import Counter

    counts: Counter = Counter()
    for r in recs:
        for v in check(store, r):
            counts[f"{v.severity}:{v.code}"] += 1
    return dict(counts.most_common())


def annotate(store: OntologyStore, rec: CaseRecord) -> CaseRecord:
    """Attach violation flags and reduce confidence. Never drops the record."""
    vs = check(store, rec)
    if not vs:
        return rec
    flags = sorted({v.as_flag() for v in vs})
    n_err = sum(1 for v in vs if v.severity == SEV_ERROR)
    conf = rec.confidence if rec.confidence is not None else 1.0
    conf *= 0.5 ** n_err
    conf *= 0.9 ** (len(vs) - n_err)
    return rec.model_copy(
        update={"qa_flags": sorted(set(rec.qa_flags) | set(flags)),
                "confidence": round(max(0.0, min(1.0, conf)), 4)}
    )
