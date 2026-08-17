"""Dictionary + negation baseline extractor. No API key, no spend, no network.

Its job is to be the honest floor. Every improvement claimed for an LLM
extractor is measured against this, on the same split, with the same metric.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from ..corpus.jats import Document
from ..ontology.grounding import GeneGrounder, Grounder, find_hgvs
from ..ontology.store import OntologyStore
from ..schema import (
    CaseRecord,
    DiagnosisAssertion,
    OntologyClass,
    PhenotypeAssertion,
    Section,
    SourceDoc,
    Subject,
    VariantAssertion,
)
from .base import is_patient_section, make_evidence


@dataclass
class BaselineConfig:
    patient_sections_only: bool = True
    keep_polarity_conflicts: bool = True
    min_mentions: int = 1
    genes_from_sections: tuple[Section, ...] = (Section.TITLE, Section.ABSTRACT, Section.BODY)
    multiword_related_synonyms: bool = True
    # Ground phenotypes only under HP:0000118 (Phenotypic abnormality). 100.00% of
    # gold phenotype terms live there; grounding into HPO's modifier/inheritance/
    # frequency branches produced 31.9% guaranteed false positives. Set False to
    # reproduce the ablation.
    restrict_to_phenotypic_abnormality: bool = True
    # A case report names many genes but implicates one. Recall for "gold gene
    # appears somewhere in the paper" is ~97%; precision is the entire problem
    # (mean 10.9 genes mentioned per paper). So rank and keep the top few rather
    # than emitting every symbol seen.
    max_genes: int = 1
    max_diseases: int = 1
    # Positional evidence weights for ranking the causal gene.
    w_title: float = 6.0
    w_abstract: float = 3.0
    w_body: float = 0.5
    w_hgvs_sentence: float = 4.0


class DictionaryExtractor:
    name = "dictionary-v1"

    def __init__(self, store: OntologyStore, config: BaselineConfig | None = None):
        self.store = store
        self.cfg = config or BaselineConfig()
        from ..ontology.grounding import HPO_PHENOTYPIC_ABNORMALITY

        self.pheno = Grounder(
            store,
            multiword_related=self.cfg.multiword_related_synonyms,
            root=(HPO_PHENOTYPIC_ABNORMALITY
                  if self.cfg.restrict_to_phenotypic_abnormality else None),
        )
        self.disease = Grounder(store, which="mondo", multiword_related=False)
        self.genes = GeneGrounder(store)

    def extract(self, doc: Document, source: SourceDoc) -> list[CaseRecord]:
        """One record per document: this baseline does not segment individuals.

        Stated rather than hidden - it is why the harness reports Track PAPER
        separately, and why Track SINGLE (one gold case per paper) is the clean
        comparison for this extractor.
        """
        sentences = doc.sentences()
        if self.cfg.patient_sections_only:
            sentences = [s for s in sentences if is_patient_section(s)]

        # --- phenotypes ----------------------------------------------------
        by_key: dict[tuple[str, bool], PhenotypeAssertion] = {}
        counts: Counter = Counter()
        polarity: dict[str, set[bool]] = defaultdict(set)
        for sent in sentences:
            for m in self.pheno.find(sent.text):
                term = self.store.hpo.normalize(m.term_id)
                if not term:
                    continue
                key = (term, m.negated)
                counts[key] += 1
                polarity[term].add(m.negated)
                ev = make_evidence(
                    sent, source, self.name,
                    start=sent.start + m.start, end=sent.start + m.end,
                )
                if key in by_key:
                    if len(by_key[key].evidence) < 5:  # cap evidence per assertion
                        by_key[key].evidence.append(ev)
                else:
                    by_key[key] = PhenotypeAssertion(
                        term=OntologyClass(id=term, label=self.store.hpo.label(term)),
                        excluded=m.negated,
                        negation_cue=m.negation_cue,
                        evidence=[ev],
                    )

        flags: list[str] = []
        phenotypes = []
        for (term, neg), assertion in by_key.items():
            if counts[(term, neg)] < self.cfg.min_mentions:
                continue
            if len(polarity[term]) > 1:
                flags.append(f"polarity_conflict:{term}")
                if not self.cfg.keep_polarity_conflicts:
                    # keep whichever polarity was asserted more often
                    if counts[(term, neg)] < counts[(term, not neg)]:
                        continue
            phenotypes.append(assertion)

        # --- genes and variants --------------------------------------------
        gene_hits: dict[str, VariantAssertion] = {}
        gene_score: dict[str, float] = defaultdict(float)
        for sent in sentences:
            if sent.section not in self.cfg.genes_from_sections:
                continue
            hits = self.genes.find(sent.text)
            hgvs_here = find_hgvs(sent.text)
            weight = {
                Section.TITLE: self.cfg.w_title,
                Section.ABSTRACT: self.cfg.w_abstract,
            }.get(sent.section, self.cfg.w_body)
            for m in hits:
                gene_score[m.term_id] += weight
                if hgvs_here:
                    gene_score[m.term_id] += self.cfg.w_hgvs_sentence
                ev = make_evidence(
                    sent, source, self.name,
                    start=sent.start + m.start, end=sent.start + m.end,
                )
                if m.term_id in gene_hits:
                    if len(gene_hits[m.term_id].evidence) < 5:
                        gene_hits[m.term_id].evidence.append(ev)
                else:
                    gene_hits[m.term_id] = VariantAssertion(
                        gene=OntologyClass(id=m.term_id, label=self.store.gene_symbol(m.term_id)),
                        evidence=[ev],
                    )
            # Attach HGVS expressions to the gene named in the same sentence.
            unique_here = {g.term_id for g in hits}
            target = gene_hits.get(next(iter(unique_here))) if len(unique_here) == 1 else None
            for kind, val, hs, he in hgvs_here:
                ev = make_evidence(sent, source, self.name, start=sent.start + hs, end=sent.start + he)
                if target is None:
                    continue
                if kind == "hgvs.c" and not target.hgvs_c:
                    target.hgvs_c = val
                elif kind == "hgvs.p" and not target.hgvs_p:
                    target.hgvs_p = val
                if len(target.evidence) < 5:
                    target.evidence.append(ev)

        ranked_genes = sorted(gene_hits, key=lambda g: (-gene_score[g], g))
        if self.cfg.max_genes:
            ranked_genes = ranked_genes[: self.cfg.max_genes]
        variants = [gene_hits[g] for g in ranked_genes]

        # --- diagnosis ------------------------------------------------------
        cand: dict[str, DiagnosisAssertion] = {}
        dis_score: dict[str, float] = defaultdict(float)
        head_sents = [s for s in sentences if s.section in (Section.TITLE, Section.ABSTRACT)]
        for sent in head_sents:
            for m in self.disease.find(sent.text):
                if m.negated:
                    continue
                mid = self.store.mondo.normalize(m.term_id)
                if not mid:
                    continue
                # Prefer the disease named in the title, and prefer specific
                # diseases over grouping terms: a paper about a named syndrome
                # mentions the broad category too, and the broad category is
                # almost never the curated diagnosis.
                specificity = len(self.store.mondo.ancestors(mid)) / 10.0
                dis_score[mid] += (
                    self.cfg.w_title if sent.section == Section.TITLE else self.cfg.w_abstract
                ) + specificity
                if mid not in cand:
                    cand[mid] = DiagnosisAssertion(
                        disease=OntologyClass(id=mid, label=self.store.mondo.label(mid)),
                        stated_in_abstract=True,
                        evidence=[
                            make_evidence(
                                sent, source, self.name,
                                start=sent.start + m.start, end=sent.start + m.end,
                            )
                        ],
                    )
        ranked_dx = sorted(cand, key=lambda d: (-dis_score[d], d))
        if self.cfg.max_diseases:
            ranked_dx = ranked_dx[: self.cfg.max_diseases]
        diagnoses = [cand[d] for d in ranked_dx]

        rec = CaseRecord(
            id=f"{source.curie.replace(':', '_')}_doc",
            source=source,
            subject=Subject(id="document", evidence=[]),
            phenotypes=phenotypes,
            diagnoses=diagnoses,
            variants=variants,
            qa_flags=sorted(set(flags)),
            extractors=[self.name],
        )
        return [rec]
