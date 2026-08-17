# Benchmarks

All numbers from `make reproduce` on this repository. Regenerate with the commands in
[REPRODUCE](REPRODUCE.md); raw output in `reports/`.

Environment: Python 3.13.0, macOS (darwin), phenopacket-store 0.1.27, ontologies fetched
2026-08-17.

---

## 1. What is being measured

### The evaluation set

| | Papers | Gold cases |
|---|---|---|
| phenopacket-store 0.1.27, total | 1,733 | 10,377 |
| Readable under our licence rules | 647 | 4,149 |
| Eval set (retracted excluded) | **646** | **4,147** |
|, Track SINGLE (one gold case per paper) | 261 | 261 |
|, Track PAPER (gold pooled per document) | 385 | 3,886 |
| Dev / test split | 309 / 337 | 1,932 / 2,215 |

### Two tracks, never averaged

The gold set is mostly multi-individual cohort papers (median 2 cases per paper, max 462), so
a single "case-level F1" would be ambiguous.

- **Track SINGLE**: papers contributing exactly one gold case. Prediction and gold are
  directly comparable with no individual segmentation involved. **This is the clean measure
  of extraction quality**, and the right track for a document-level extractor.
- **Track PAPER**: all papers, gold pooled per document. Segmentation-free, measures
  corpus-scale recall, systematically kinder on recall and harsher on precision.

### Metrics

**Primary: `observed_phenotype_graded_f1`.** Best-match graded F1 over *present* phenotypes,
using Lin similarity over the HPO DAG with information content from `phenotype.hpoa`
(285,598 annotations). Each predicted term earns its best similarity to any gold term
(precision side); each gold term earns its best similarity to any predicted term (recall
side). Asymmetric on purpose - one very general prediction should not earn full recall over
ten specific gold terms.

**Similarity threshold 0.5.** Unrelated HPO terms share high-level ancestors
(`lin(Seizure, Microcephaly) = 0.258`), so ungated partial credit gives a wholly wrong
prediction ~0.26 for free and makes an F1 ≥ 0.85 target partly reachable by coincidence.
Thresholded, a deliberately unrelated prediction scores exactly 0.000.

**Reported alongside, never as a substitute:** exact-match F1; absent-phenotype exact F1
(scored as a separate population - 59.1% of gold features are `excluded: true`); gene F1
(canonical HGNC symbols); diagnosis F1 (MONDO-normalised).

**Bootstrap CIs.** 1,000 resamples over cases, fixed seed, percentile method.

---

## 2. Baseline results

Dictionary + NegEx-style negation, ranked gene/disease selection. No API key.

| Group | n | Obs exact F1 | **Obs graded F1** | Graded 95% CI | Absent F1 | Gene F1 | Diagnosis F1 |
|---|---|---|---|---|---|---|---|
| all / all | 646 | 0.4313 | 0.5926 | [0.573, 0.612] | 0.0759 | 0.8846 | 0.1700 |
| all / dev | 309 | 0.4255 | 0.5851 | [0.556, 0.612] | 0.0755 | 0.8903 | 0.1718 |
| all / test | 337 | 0.4365 | 0.5992 | [0.570, 0.629] | 0.0763 | 0.8794 | 0.1684 |
| paper / all | 385 | 0.4404 | 0.6064 | [0.581, 0.631] | 0.0680 | 0.9037 | 0.1204 |
| paper / dev | 180 | 0.4304 | 0.5947 | [0.561, 0.628] | 0.0660 | 0.8926 | 0.1209 |
| paper / test | 205 | 0.4494 | 0.6171 | [0.582, 0.655] | 0.0699 | 0.9135 | 0.1199 |
| **single / all** | 261 | 0.4055 | **0.5581** | [0.526, 0.587] | 0.1133 | 0.8560 | 0.2456 |
| single / dev | 129 | 0.4110 | 0.5597 | [0.530, 0.592] | 0.1186 | 0.8872 | 0.2451 |
| single / test | 132 | 0.4008 | 0.5568 | [0.511, 0.602] | 0.1079 | 0.8258 | 0.2462 |

### Reading these

- **The dev/test gap is noise.** Track SINGLE differs by 0.003 with a CI half-width of ~0.045.
  The split is not adversarial, and there is no evidence of dev overfitting.
- **Absent-phenotype F1 (0.11) is the worst dimension** and the primary metric does not cover
  it. Since 59% of gold features are absent findings, a reader who takes 0.56 as "how good is
  extraction" is being over-optimistic by a wide margin.
- **Track PAPER scores higher than Track SINGLE** (0.606 vs 0.558) exactly as the design
  predicts: pooling gold per document rewards a document-level extractor. Do not compare
  across tracks.
- **Diagnosis F1 (0.25) has a hard ceiling of ~0.988** because 1.2% of gold diagnoses have
  no MONDO equivalent. The remaining gap is real weakness.
- **Gene F1 is the one strong dimension** (0.826 test / 0.887 dev). The dev/test spread here
  is wider than for phenotypes because gene F1 is near-binary per paper on a 130-paper split.

### Against the project target

The plan targets F1 ≥ 0.85 on phenotypes, gene, and diagnosis. Current state:

| Field | Target | Baseline (SINGLE/test) | Gap |
|---|---|---|---|
| Phenotypes (graded) | 0.85 | 0.557 | −0.293 |
| Phenotypes (exact) | 0.85 | 0.401 | −0.449 |
| Gene | 0.85 | 0.826 | −0.024 |
| Diagnosis | 0.85 | 0.246 | −0.604 |
| Absent phenotypes |, | 0.108 | (no target set; should have one) |

A dictionary is already within 0.02 of target on genes. Phenotypes and diagnosis are where an
LLM extractor has to earn its cost, and absent-findings recall is where it has the most room.
The plan sets no target for absent findings even though they are 59% of the gold data - that
is a gap in the plan, not just in the implementation.

---

## 3. Ablations

Track SINGLE / all, so the numbers are comparable to the 0.5581 baseline above.

| Configuration | Obs graded F1 | Δ vs baseline | 95% CI |
|---|---|---|---|
| Baseline | **0.5581** |, | [0.526, 0.587] |
| Phenotype-branch restriction **off** (`--no-phenotype-root`) | 0.4404 | **−0.1177** | [0.414, 0.465] |
| Patient-section filter **off** (`--all-sections`) | 0.5198 | **−0.0383** | [0.493, 0.543] |
| Multi-word RELATED synonyms **off** (`--no-related-synonyms`) | 0.5582 | +0.0001 | [0.526, 0.586] |

**Restricting grounding to `HP:0000118` is by far the biggest lever: +0.118 graded F1.**
HPO contains real terms that are not phenotypes - "Affected", "Bilateral", "Autosomal
dominant inheritance" - and 100.00% of gold phenotype terms live under Phenotypic
abnormality while only 68.13% of pre-fix predictions did. Nearly a third of every phenotype
assertion was a guaranteed false positive. See [DECISIONS D21](DECISIONS.md).

**The section filter earns its place.** Excluding Introduction/Discussion/Methods is worth
+0.038 graded F1, intervals barely overlapping. Papers really do contaminate extraction by
discussing other papers' patients.

**The synonym-scope decision does not.** Admitting multi-word BROAD/RELATED synonyms moves
F1 by **−0.0001**: 1,952 extra phrases for nothing measurable. The qualitative argument
stands (an extractor that cannot read "hearing loss" is deficient on its face) but the
measurement gives it no support, and it is recorded as **unsupported by measurement** in
[DECISIONS D10](DECISIONS.md). Reporting an ablation that failed to confirm a design choice
is the point of running it.

---

## 4. Quality audit

`make qa` → `reports/qa_audit.json`. Baseline predictions across 646 papers.

### Provenance verification

| | |
|---|---|
| Assertions checked | 32,173 |
| Cited span verifiably supports the assertion | 31,989 (**99.43%**) |
| Unsupported | 184 (**0.57%**), all `polarity_mismatch` |

**Interpret with care.** For a dictionary extractor this is close to tautological - the term
was found *by* matching that span, so a high rate is expected. Its value is as a measurement
floor and as the number that becomes a genuine hallucination rate once an LLM extractor runs.
The 228 failures are real disagreements: the span grounds to the claimed term but re-reading
the surrounding context yields a different polarity.

### Machine-checkable constraint violations

| Code | Severity | Count | Meaning |
|---|---|---|---|
| `polarity_contradiction` | ERROR | 639 | Same term asserted present and absent |
| `dag_polarity_contradiction` | ERROR | 623 | Specific finding present while its ancestor is absent |
| `gene_disease_mismatch` | WARN | 63 | Gene not a known cause of the asserted disease |
| `obligate_phenotype_excluded` | WARN | 10 | An obligate feature of the diagnosis recorded absent |

The 1,262 polarity ERRORs are **not** a negation bug. The baseline emits one record per
document, so a term present for individual 1 and absent for individual 2 collapses into a
self-contradicting subject. The checker is correct; the extractor is structurally wrong. This
is the quantified cost of not segmenting individuals.

### Distant supervision

633 of 646 papers agree between the diagnosis stated in the title/abstract and the diagnosis
extracted; 13 state none. **This is a regression detector, not a benchmark**: papers that
state the answer up front are exactly the easy ones, so a high score here says little about
hard cases.

### Retractions

94,265 Retraction Watch notices indexed. One eval-set paper is retracted (PMID 30850397 -
image duplication, IRB failure), caught independently by the PMC OA `retracted` attribute and
by Retraction Watch. Excluded from the eval set by default; flagged, never deleted.

---

## 5. Gold-set audit

Running the constraint checker over the 4,149 **expert-curated** cases:

| Code | Severity | Count |
|---|---|---|
| `obligate_phenotype_excluded` | WARN | 962 |
| `onset_after_last_encounter` | ERROR | 77 |
| `unmappable_disease` | WARN | 49 |
| `outdated_hpo_term` | WARN | 10 |

The 77 onset errors are genuine internal inconsistencies in expert data (encounter `P18Y`
with onset `P19Y`; encounter `P1D` with onset `P1M21D`), spot-checked against raw records.
Zero polarity contradictions: the curators are internally consistent on polarity, which is
the strongest available validation of the checker's ERROR tier.

---

## 6. Diagnostic ranking: what extraction error costs

`make diagnostic`. A transparent Phenomizer-style ranker (`rdcd/eval/diagnose.py`) scores
10,089 candidate diseases by information content shared with the query phenotypes, then two
conditions are run over the same papers with the same target:

- **CEILING**: query built from the expert gold phenotypes. The best this ranker can do if
  extraction were perfect.
- **PIPELINE**: query built from our extracted phenotypes. The real end-to-end number.

**CEILING − PIPELINE is what extraction error costs diagnosis, in top-k recall**: and
therefore whether the next month belongs to extraction or to the ranker. Ceiling also bounds
the ranker itself: if ceiling top-20 is low, better extraction cannot rescue it.

Track SINGLE, 258 papers scored (3 excluded: no MONDO-mappable gold diagnosis).

| Condition | top-1 | top-3 | top-5 | top-10 | top-20 | MRR |
|---|---|---|---|---|---|---|
| **Ceiling** (gold phenotypes) | 0.523 | 0.628 | 0.686 | 0.733 | 0.798 | 0.599 |
| **Pipeline** (extracted phenotypes) | 0.229 | 0.411 | 0.469 | 0.566 | 0.659 | 0.345 |
| **Cost of extraction error** | **−0.295** | −0.217 | −0.217 | −0.167 | −0.140 | −0.254 |

### What this settles

**Extraction is the bottleneck, not the ranker.** Given expert phenotypes, a deliberately
simple information-content ranker puts the right disease first 52% of the time and in the
top 20 80% of the time. Given our extracted phenotypes it manages 23% and 66%. Extraction
error costs **29.5 percentage points of top-1 recall** and 0.25 MRR.

So the answer to "should the next month go into extraction or into the ranker?" is
extraction, and it is not close. It also bounds the ranker honestly: ceiling top-20 of 0.798
means even perfect extraction leaves 20% of cases unranked in the top 20 by this method, so
a better ranker is eventually needed - just not first.

### The fix propagated, and the experiment validated itself

This benchmark was run twice, before and after the D21 phenotype-branch fix. The **ceiling
row is byte-identical** across both runs - exactly as it must be, since gold phenotypes are
untouched by a grounder change. Only the pipeline row moved, which is what makes the
comparison trustworthy rather than a coincidence:

| | Pre-D21 | Post-D21 | Δ |
|---|---|---|---|
| Pipeline top-1 | 0.140 | 0.229 | **+0.089** (+64% rel.) |
| Pipeline top-3 | 0.225 | 0.411 | **+0.186** (+83% rel.) |
| Pipeline top-20 | 0.566 | 0.659 | +0.093 |
| Pipeline MRR | 0.231 | 0.345 | **+0.114** (+49% rel.) |
| Cost of extraction error (top-1) | −0.384 | −0.295 | gap narrowed 8.9 pts |

A +0.118 phenotype-F1 improvement bought +0.089 top-1 and +0.114 MRR of *downstream
diagnostic* gain. That link is the reason to maintain a ceiling/pipeline benchmark at all:
it converts an intrinsic extraction metric into the quantity anyone actually cares about,
and it would have exposed a phenotype "improvement" that failed to help diagnosis.

Sanity check on the ranker itself: the NF1 query (`Multiple cafe-au-lait spots`,
`Plexiform neurofibroma`, `Cafe-au-lait spot`, `Neurofibroma`) returns
neurofibromatosis-family diseases in the top 5, with NF1 itself at rank 9.

---

## 7. What the abstract-only licence tier costs

The licence audit (§1) says 1,086 of 1,733 gold source papers are `abstract_only`: we may
read their title, abstract, and metadata, but not their full text. That is a wall unless you
put a number on it. So: take the papers whose full text we *are* allowed to read, and extract
from them twice.

Same 441 quotable-tier papers, same extractor, same gold, only the input differs.
`make eval --tier full_text_quotable` vs the same with `--abstract-only`.

| Track | Condition | Phenotype graded F1 | Absent F1 | Gene F1 | Diagnosis F1 |
|---|---|---|---|---|---|
| SINGLE (166) | full text | 0.5510 | 0.1182 | 0.8373 | 0.2553 |
| SINGLE (166) | abstract only | 0.2981 | 0.0089 | 0.8528 | 0.2553 |
| | **change** | **-0.253 (-46%)** | **-0.109 (-92%)** | **+0.016** | **0.000** |
| PAPER (275) | full text | 0.6050 | 0.0624 | 0.9016 | 0.1095 |
| PAPER (275) | abstract only | 0.2143 | 0.0043 | 0.8901 | 0.1095 |
| | **change** | **-0.391 (-65%)** | **-0.058 (-93%)** | -0.012 | 0.000 |

### What this means for the 63% we cannot read in full

**Gene and diagnosis extraction survive intact.** Gene F1 is unchanged and marginally
*better* from abstracts alone (0.837 -> 0.853): the causal gene is almost always named in the
title or abstract, and dropping the body removes false-positive gene mentions from
discussions. Diagnosis F1 is identical to four decimal places, because the baseline already
grounds diagnoses only in the title and abstract by design.

**Phenotype extraction retains about half its signal** on single-case papers (0.551 ->
0.298, 54% of the full-text figure) and about a third on cohort papers (0.605 -> 0.214).
The Track PAPER drop is steeper for a structural reason: multi-individual papers keep their
per-patient phenotype grids in body tables, which abstracts do not contain at all.

**Absent findings effectively do not exist in abstracts.** F1 falls from 0.118 to **0.0089**,
a 92% loss. This is not degradation, it is absence: abstracts report what was found, not what
was looked for and ruled out. Any record from the `abstract_only` tier must be published with
an explicit flag that its negative findings are missing rather than negative, or a consumer
will read "not present in the record" as "reported absent". That distinction is the entire
point of modelling `excluded` as a positive assertion.

### Does abstract-tier phenotype data still help diagnosis?

F1 says half the phenotype signal survives. The question that actually decides shipping is
whether that half still ranks the right disease. `make diagnostic-abstract`, same 258 papers,
same ranker, same ceiling:

| Condition | top-1 | top-3 | top-5 | top-10 | top-20 | MRR |
|---|---|---|---|---|---|---|
| Ceiling (gold phenotypes) | 0.523 | 0.628 | 0.686 | 0.733 | 0.798 | 0.599 |
| Pipeline, full text | 0.229 | 0.411 | 0.469 | 0.566 | 0.659 | 0.345 |
| Pipeline, abstract only | 0.082 | 0.172 | 0.232 | 0.331 | 0.425 | 0.158 |

**Retention of full-text performance is strongly k-dependent:**

| | top-1 | top-10 | top-20 | MRR |
|---|---|---|---|---|
| Abstract-only as a share of full-text | **36%** | 58% | **64%** | 46% |

**Abstract-tier data shortlists but does not pinpoint.** It keeps 64% of full-text top-20
recall and only 36% of top-1. Fewer phenotypes still narrow the candidate set usefully, but
not enough to rank one disease first. So the honest framing of what this tier supports is
*candidate generation*, not *diagnosis*: the right answer is in the top 20 for 4 cases in 10,
against 6.6 in 10 with full text and 8 in 10 with expert phenotypes.

**A 10% hard-failure rate that F1 hides.** Only **233 of 258** abstract-only papers produced
a query at all: 25 papers yielded no groundable phenotype from the abstract, so they cannot be
ranked at any k. F1 aggregates over papers that produced output and never shows this; the
per-condition `no_query` count in the report does.

**Verdict: abstract-tier extraction is worth shipping**, with per-field expectations set
honestly. It delivers full-quality gene and diagnosis data, roughly half the phenotype signal,
and 64% of full-text top-20 diagnostic retention, for 1,086 papers otherwise contributing
nothing. Three conditions on publishing it:

1. Records carry their tier, and are not scored in the same pool as full-text records.
2. Absent findings are flagged as **missing, not negative** (F1 0.0089 is absence).
3. The tier is described as supporting candidate shortlisting, not top-1 diagnosis, and
   ~10% of its records will have no phenotypes at all.

## 8. What no benchmark here measures

- **LLM extractor accuracy.** It has never made a live API call (no API key in the build
  environment). Unit-tested offline; accuracy unmeasured.
- **Corpus-scale performance.** All results are on 646 gold-set papers, not the 2.57M
  `case reports[pt]` in PubMed.
- **Downstream clinical utility.** Top-k recall against a curated diagnosis is not the same
  as helping a clinician, and nothing here claims otherwise.
- **Cross-dataset comparison.** Not yet evaluated against PMC-Patients, RareArena, or
  PhenoBrain's released sets, so no claim of state of the art is made or implied.
