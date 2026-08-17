# Requirements

Two sections, deliberately separated by how well evidenced they are.

**§1 is grounded**: every requirement traces to a measurement in this repository, and the
script that produced it is named.

**§2 is not done.** The project plan calls for requirements synthesised from published
usability and failure-mode literature. That work needs the source papers read. The sources
are named below and the questions they should answer are written out, but **no findings are
attributed to any paper here.** Inventing what a paper says would be worse than admitting
the gap.

---

## §1. Requirements established by measurement

### R1. Model explicitly-absent findings, or be wrong about the majority of the target
59.1% of phenotype features in expert phenopackets are `excluded: true` (92,068 observed vs
132,795 excluded across 10,377 cases). *Source: `scripts/audit_goldset.py`, gold-set scan.*
→ Schema models absence as a positive assertion; metrics score it separately; the current
absent-phenotype F1 of 0.10 is the largest known quality gap (ERROR_LEDGER E2).

### R2. Segment individuals, because the literature is cohort-shaped
10,377 gold cases come from 1,733 papers - median 2 individuals per paper, maximum 462. Only
666 papers describe exactly one individual. *Source: gold-set grouping,
`rdcd/eval/goldsets.py`.*
→ Document-level extraction is structurally unable to serve the domain. Measured cost of not
doing it: 1,845 polarity contradictions across 646 papers (ERROR_LEDGER E3).

### R3. Extract from tables, not just prose
Cohort papers report per-patient phenotypes in tables. In the eval set, table content is a
substantial share of extracted spans and is where multi-individual grids live. *Source:
`rdcd/corpus/jats.py` section counts.*
→ Tables are parsed as first-class content and split line-wise, not sentence-wise.

### R4. Plan around the open-access ceiling, and state it
Of 1,733 gold source papers: 1,023 in PMC, 647 in the OA subset, 442 permitting quoted
evidence. Any coverage claim must be stated against 647, not against PubMed's 2,572,683
`case reports[pt]`. *Source: `reports/goldset_availability.json`.*
→ Licence tier is computed per source at fetch time and stored on every record.

### R5. Make verification possible without clinicians
No clinical reviewer is available to this project. *Constraint, not a finding.*
→ Provenance-or-null reduces audit to reading comprehension; `rdcd/qa/provenance.py`
mechanises it and reports a support rate (currently 99.43% over 32,173 assertions).

### R6. Score ontology-aware, with a floor guard
Exact HPO-ID matching penalises correct extractions that chose a sibling or child term; but
unrelated HPO terms share high-level ancestors, so ungated partial credit gives a wrong
prediction ~0.26 for free. *Source: `tests/test_metrics.py`, `lin_similarity` measurements.*
→ Primary metric is graded F1 with a 0.5 similarity threshold; exact F1 reported alongside.

### R7. Normalise vocabularies before scoring
Gold diagnoses are OMIM; HPO annotations use OMIM and Orphanet; 98.8% of gold diagnosis
assertions normalise to MONDO. *Source: `rdcd/ontology/store.py` normalisation scan.*
→ All disease identifiers normalise to MONDO before comparison, so a correct answer in the
wrong vocabulary is not scored as wrong.

### R8. Detect retractions from more than one source
One retracted paper sits inside the expert gold set (PMID 30850397), caught by the PMC OA
`retracted` attribute and independently confirmed by Retraction Watch. *Source:
`reports/qa_audit.json`.*
→ Three independent sources; retracted records are flagged, never deleted.

### R9. Ranked selection for single-answer fields
Recall for "the causal gene appears somewhere in the paper" is 97% (68/70 papers); precision
was the problem at mean 10.9 gene mentions per paper. *Source: gene-behaviour diagnostic
during baseline development.*
→ Positional + HGVS-co-occurrence ranking, top-1 by default. Moved gene F1 from 0.17 to 0.89.

### R10. Ground only into the ontology branch the target actually uses
100.00% of the 90,549 gold phenotype terms are descendants of `HP:0000118` (Phenotypic
abnormality); zero are outside it. Grounding into HPO's modifier, inheritance, and frequency
branches made 31.9% of predicted assertions guaranteed false positives. *Source: gold-set and
prediction term-distribution scan; ablation `--no-phenotype-root`.*
→ The grounder is root-restricted by default; fixing this was worth +0.118 graded F1, the
largest single improvement measured. Generalisation: **for every field, check the ontology
subtree the gold data actually occupies before grounding into the whole ontology.**

### R11. Look at rendered records, not only at aggregate scores
R10 was invisible to the primary metric for the whole development period and invisible to
the test suite. It became obvious within seconds of rendering real records in the reference
UI. *Source: how E10 was actually found.*
→ The thin reference UI is a debugging instrument, not only a deliverable, and should exist
from the first extraction run rather than at the end.

### R12. Reproducibility requires a cache, not just a seed
Scoring against a live API means numbers change when the API's content changes.
→ All network I/O is content-addressed and cached; scoring targets read only the cache.

---

## §2. Not done: synthesis from published literature

The plan's §1 replaces clinician interviews with published evidence of need. That
substitution is sound, and the synthesis has **not** been performed. What follows is the
reading list and the questions to put to it - not answers.

### Sources to read
- **FindZebra**: usability and evaluation papers on rare-disease search.
- **DeepRare**: published failure-mode analysis. The plan asserts specific error modes
  (over-weighting nonspecific symptoms, confusing similar syndromes); **verify these against
  the paper before using them to prioritise anything.** They are currently unverified claims
  in a plan, not findings.
- **PhenoBrain**: released case sets and evaluation protocol.
- **LIRICAL / Exomiser**: phenotype-driven prioritisation evaluation methodology; directly
  relevant to whether `rdcd/eval/diagnose.py` is a defensible baseline.
- **PMC-Patients** (~167k) and **RareArena** (~70k) - existing structured case collections.
- **RAMEDIS**: curated case data.
- Diagnostic-odyssey surveys (e.g. Shire/EURORDIS-style) for time-to-diagnosis evidence.
- GA4GH Phenopackets specification papers, for schema conformance rather than motivation.

### Questions the reading must answer
1. **Does the gap this project assumes actually exist?** PMC-Patients and RareArena already
   provide large structured case collections. The differentiator must be stated precisely -
   candidate answer from this repo's own work: per-field provenance with verifiable offsets,
   phenopacket-native output, explicit licence tiering, and versioned releases. That claim
   needs checking against what those datasets already publish. **This is the single highest-value
   open question, and if the answer is "no", the project should stop.**
2. What retrieval and presentation behaviours do clinicians actually use in rare-disease
   search, and which failures are reported repeatedly?
3. Which published error taxonomy should drive extraction priorities, and does it match the
   error classes this repository has independently measured (ERROR_LEDGER)?
4. What evaluation protocols are already standard, so our benchmark is comparable rather
   than bespoke?
5. What licence terms do the existing collections carry, and are they compatible with a
   CC-BY derived dataset?

### Method when it is done
Cite every requirement to a specific paper and section. Mark anything inferred rather than
stated as inferred. Where this repository's measurements contradict the literature, report
both - a disagreement between a published claim and a reproducible measurement is a finding
in itself.
