# Public error ledger

Every known error class, its measured rate, and its status. Trust without conversation
requires published measurement, so this file exists to be unflattering.

Measured on the dictionary baseline over 646 eval papers unless stated. Regenerate with
`make qa` (`reports/qa_audit.json`) and `make eval` (`reports/eval_baseline.json`).

---

## Our errors

### E1. Missed phenotypes (false negatives) - the largest error class
**Rate.** Observed-phenotype graded F1 0.56 / exact 0.40 on Track SINGLE. A substantial
share of curated present-findings is still not recovered.
**Cause.** Dictionary matching only fires on a surface form present in HPO's label or
synonym set. Paraphrase ("could not sit unsupported" → `HP:0002540`), findings implied
across two sentences, and findings stated only as a lab value are all invisible.
**Status.** Open. This is the gap the LLM extractor exists to close.

### E2. Negation recall is poor
**Rate.** Absent-phenotype F1 **0.11** on Track SINGLE, against 0.56 for present findings.
Since 59% of gold features are absent, this is the single worst-performing dimension.
**Cause.** NegEx-style cue matching catches explicit local negation but not tabular
notation (`−`, `N`, `absent` in a grid cell), scope over conjunctions ("no seizures,
microcephaly or ataxia" - only the first is caught), or clinical idiom ("unremarkable").
**Status.** Open, and under-weighted by the headline metric. Do not read the primary F1 as
covering this.

### E3. Polarity contradictions at document level
**Rate.** 639 `polarity_contradiction` + 623 `dag_polarity_contradiction` ERRORs across
646 papers (down from 1,045 + 805 after E10).
**Cause.** Not a negation bug. The baseline emits **one record per document**, so a term
present for individual 1 and absent for individual 2 collapses into a single self-contradicting
subject. The constraint checker is right to flag it; the extractor is wrong to produce it.
**Status.** Expected and quantified. It is the sharpest available argument for
per-individual segmentation, and the number to beat.

### E4. Diagnosis extraction is weak
**Rate.** Normalised-diagnosis F1 **0.25** (Track SINGLE), 0.12 (Track PAPER).
**Cause.** Grounding disease names by matching MONDO labels in the title/abstract picks up
grouping terms and comorbidities; a specificity heuristic plus top-1 ranking improved it
from 0.12 to 0.25 but it remains the least developed component.
**Status.** Open.

### E5. Unsupported evidence spans
**Rate.** **0.57%** of 32,173 assertions fail re-verification - 184 cases, all
`polarity_mismatch` (the cited span grounds to the claimed term, but re-reading the
surrounding context yields a different polarity than recorded).
**Caveat that matters.** For a dictionary extractor this rate is near-tautological: the
term was found *by* matching that span. It is a floor on measurement quality, not evidence
that extraction is 99.5% correct.
**The LLM number now exists** (BENCHMARKS §8, n=7): **0 of 98 quotes were not found in the
document, a 0.0% hallucination rate.** That is the number this check was built to produce,
and the quote-based design earned it. Small n, but the mechanism is structural rather than
statistical: a quote is either present or it is not.
**Status.** Measured on both paths.

### E6. Gene-symbol collisions with disease abbreviations
**Rate.** Was ~3 per 60 papers before the fix; 0 observed after.
**Cause.** Case-sensitive symbol matching resolved "LCA" (Leber congenital amaurosis) to
`GUCY2D` and "KS" (Kabuki/Kallmann syndrome) to `OXSM` via HGNC aliases.
**Status.** Fixed by an explicit blocklist of clinical abbreviations
(`grounding.GeneGrounder.AMBIGUOUS`), found *by* the provenance audit - the QA layer
catching a real extractor bug is the mechanism working as designed.

### E7. Gene anchored on the wrong spelling
**Rate.** Unmeasured at scale; reproduced on a real paper.
**Cause.** The LLM extractor located its gene anchor by searching for the approved HGNC
symbol. A paper writing `FOG2` for `ZFPM2` produced a correct gene that was then silently
dropped for want of an anchor.
**Status.** Fixed - anchors now try every HGNC alias and previous symbol, and quote the
spelling the paper actually uses.

### E8. Ungroundable findings (LLM path) - measured, and it was the dominant failure
**Rate.** **46.8%** with quote-only grounding; **2.2%** after adding the `label` field.
**Cause.** By design (D3) the model returns text, not identifiers, so a correctly-extracted
phrase the grounder cannot map is discarded. Measured on real papers, this was not a minor
tax: it discarded nearly half of all findings and **100% of absent findings**, because prose
states absence as "X was normal", which contains no ontology term to match.
**Fix.** Findings carry a normalised `label` (a clinical term) alongside the verbatim quote;
the label is grounded, the quote still anchors provenance. Identifiers still come only from
the ontology, so hallucination-proofing is preserved: it stayed at 0.0%.
**Status.** Fixed and measured. Had this not been instrumented separately from the F1, the
whole loss would have been misread as the model extracting badly.

### E10. Grounding into non-phenotype HPO branches - **the largest error found, now fixed**
**Rate before fix.** **31.9%** of all phenotype assertions (2,063 of 6,473 over 200 papers).
**Rate after fix.** 0% by construction.
**Cause.** HPO contains real terms that are not phenotypic abnormalities: modifiers
(`Left`, `Right`, `Bilateral`, `Peripheral`, `Recurrent`), status terms (`Affected`,
`Unaffected`, `Healthy`), `Family history`, `Frequency`, and inheritance modes
(`Autosomal dominant inheritance`). The grounder matched them happily. A phenopacket's
`phenotypicFeatures` never contains them - 100.00% of the 90,549 gold phenotype terms are
`HP:0000118` descendants, and zero are outside it - so every such assertion was a guaranteed
false positive.
**Effect of the fix.** Track SINGLE graded F1 0.4404 → **0.5581** (+0.1177); exact
0.3162 → 0.4055; polarity-contradiction ERRORs −39%; release assertion count −29%.
**How it was found.** By rendering the reference UI and reading the phenotype chips on real
records - not by the metric, which reported 0.44 without complaint, and not by a test. An
aggregate score cannot tell you the units it aggregates are the wrong *kind* of thing.
Evidence: [before](img/ui_before_phenotype_root_fix.png) (chips read `Affected`,
`Bilateral`, `Autosomal dominant inheritance`) and
[after](img/ui_after_phenotype_root_fix.png) (same query, only real phenotypes).
**Status.** Fixed (DECISIONS D21), reproducible as an ablation
(`--no-phenotype-root`), with a regression test.

### E9. Sentence splitting on clinical abbreviations
**Rate.** Not systematically measured; abbreviation list covers ~60 common cases.
**Cause.** `Fig.`, `et al.`, `p.Leu12Ter`, `NM_138961.3` all contain periods. A bad split
truncates the sentence a citation points into.
**Status.** Mitigated by an abbreviation blocklist and single-initial rule; offsets are
verified exact for every sentence in the parser test.

---

## Methods hazards

Not bugs in this code, but failure modes of measurement that this project walked into and
now guards against. Named so they can be cited and checked for elsewhere.

### H1. Metrics averaged over successful cases are blind to total failures
**Where it bit us.** The abstract-only diagnostic benchmark reported top-20 recall 0.4249.
That figure is averaged over the 233 papers that produced a query. 25 further papers (10%)
yielded no groundable phenotype at all and were silently excluded from the denominator. The
figure a corpus user actually experiences is **0.3837**. The conditional number overstated
performance by 11% relative, and nothing in the metric surfaced it.

**Why it is easy to miss.** The pattern is structural, not careless: any per-case average
implicitly conditions on the case producing an output to average. Precision, recall, F1, MRR,
and top-k all do this. A pipeline that fails outright on 10% of inputs and does well on the
rest is indistinguishable, in those metrics, from one that handles everything at slightly
lower quality. The two have very different value.

**The guard.** Every rate in this repository is reported twice where the two can differ:
conditional (averaged over cases that produced output) and **unconditional** (failures count
as misses), plus an explicit `coverage` figure. `run_diagnostic_benchmark.py` emits
`topk_recall`, `topk_recall_unconditional`, and `coverage` side by side. Quote the
unconditional figure whenever the claim is about corpus coverage.

**Generalisation.** Report the denominator, not just the ratio. If a benchmark cannot tell
you how many inputs produced no output at all, it is not reporting performance, it is
reporting performance-given-success.

### H2. Ratios measured under one extractor are not properties of the data
**Where it bit us.** The abstract-vs-full-text retention ratios (32% of top-1, 58% of top-20)
were initially written as if they described the `abstract_only` licence tier. They describe
that tier **under a dictionary extractor**. A different extractor has a different negation
profile and a different tolerance for dense prose, so it may retain a materially different
fraction from abstracts.

**The guard.** The tier table in BENCHMARKS §7 is labelled with the extractor that produced
it, and re-running it is a listed action for whenever a new extractor lands. Cheap to
regenerate (`make eval --tier ... --abstract-only`, `make diagnostic-abstract`); the risk is
letting one extractor's ratios calcify into strategy as if they were constants.

## Errors found in the expert gold data

Reported for transparency and worth sending upstream. These are not our errors, but they
bound how high any score can go.

### G1. Onset later than age at last encounter - 77 cases
Internally inconsistent expert phenopackets: encounter `P18Y` with onset `P19Y`;
encounter `P1D` with onset `P1M21D`. Spot-checked against the raw records; the ISO parser
handles years, months, weeks, and days, so these are genuine.

### G2. Obligate phenotype recorded absent - 962 cases
A feature HPO annotates as obligate (100% frequency) for the diagnosed disease is recorded
as explicitly absent. Some are curation errors; some are HPO frequency annotations that are
too strong. Severity WARN, not ERROR, for that reason.

### G3. Unmappable diagnoses - 49 assertions
OMIM identifiers with no MONDO equivalent (e.g. `OMIM:601674`, `OMIM:621570`). 1.2% of gold
diagnosis assertions, so diagnosis F1 has a hard ceiling of ~0.988.

### G4. Outdated HPO terms - 10 assertions
Alias or obsolete term identifiers that resolve via `replaced_by`. Handled transparently by
`Ontology.normalize`; flagged so drift stays visible.

---

## Systematic limits that are not bugs

- **Licence ceiling.** Only 647 of 1,733 gold source papers are readable in full
  (LICENSING). No extraction improvement changes this.
- **Distant supervision measures the easy cases.** 633/646 papers agree between the stated
  and extracted diagnosis - but papers that state the answer in the abstract are exactly
  the easy ones. This is a regression detector, not a benchmark.
- **Sex-linked constraints are effectively unavailable.** 284,958 of 285,598 HPO annotation
  rows have an empty `sex` field (99.8%), so only one disease qualifies as sex-constrained.
  Checks run off inheritance-mode annotations (8,404 diseases) instead.
- **No measured LLM accuracy.** The LLM extractor has never executed a live request. Any
  claim about its accuracy would be fabricated.
