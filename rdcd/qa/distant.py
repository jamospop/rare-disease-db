"""Distant supervision: free validation from diagnoses stated in title/abstract.

When a paper's title or abstract names the diagnosis outright, we get a label for
that document at zero annotation cost. Two uses:

  * validate the pipeline's diagnosis field at corpus scale, on papers that are
    not in any gold set;
  * find papers where the body-derived diagnosis contradicts the stated one,
    which is a strong signal of an extraction error.

The catch, stated plainly: this validates only the *easy* diagnoses. Papers that
state the answer up front are exactly the ones a diagnosis extractor finds
easiest, so a high distant-supervision score is not evidence of good performance
on hard cases. It is a regression detector, not a benchmark.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..corpus.jats import Document
from ..ontology.grounding import Grounder
from ..ontology.store import OntologyStore
from ..schema import CaseRecord, Section


@dataclass(slots=True)
class DistantLabel:
    source_id: str
    stated_diseases: tuple[str, ...]
    agrees: bool | None
    detail: str


class DistantSupervisor:
    def __init__(self, store: OntologyStore):
        self.store = store
        self.disease_grounder = Grounder(store, which="mondo", multiword_related=False)

    def stated_diagnoses(self, doc: Document) -> set[str]:
        out: set[str] = set()
        for s in doc.sentences():
            if s.section not in (Section.TITLE, Section.ABSTRACT):
                continue
            for m in self.disease_grounder.find(s.text):
                if m.negated:
                    continue
                mid = self.store.mondo.normalize(m.term_id)
                if mid:
                    out.add(mid)
        return out

    def check(self, doc: Document, rec: CaseRecord) -> DistantLabel:
        stated = self.stated_diagnoses(doc)
        pred = {d for d in (self.store.normalize_disease(x) for x in rec.disease_ids) if d}
        if not stated:
            return DistantLabel(rec.source.curie, (), None, "no diagnosis stated up front")
        if not pred:
            return DistantLabel(rec.source.curie, tuple(sorted(stated)), False,
                                "diagnosis stated in abstract but none extracted")
        # Credit a match when the prediction is the stated disease or a relative
        # of it: MONDO grouping terms and their children are both defensible.
        for pd in pred:
            for sd in stated:
                if pd == sd or pd in self.store.mondo.ancestors(sd) \
                   or sd in self.store.mondo.ancestors(pd):
                    return DistantLabel(rec.source.curie, tuple(sorted(stated)), True,
                                        f"{pd} consistent with stated {sd}")
        return DistantLabel(rec.source.curie, tuple(sorted(stated)), False,
                            f"extracted {sorted(pred)} disjoint from stated {sorted(stated)}")
