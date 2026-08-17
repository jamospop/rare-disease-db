# Design decisions

Each entry: the decision, the alternative, and what it cost. Decisions that were settled
by measurement rather than taste say so and name the script.

---

## D1. Build the eval harness before any extraction code

**Decision.** Wire the gold set, the metric, and the dev/test split up first; write the
extractor second.

**Why.** When the target is defined after the system, the target quietly becomes "whatever
the system produces." Fixing the metric first made three later choices measurable instead
of arguable (D6, D7, D10).

**Cost.** The first useful number arrived late. Worth it: the number meant something.

---

## D2. Provenance-or-null, enforced at the schema level

**Decision.** Every assertion carries ≥1 `Evidence` with a character offset. Assertions
without evidence are dropped by `CaseRecord.enforce_provenance()`, not flagged.

**Why.** It converts medical verification into reading comprehension. An auditor who
cannot judge "is this diagnosis correct?" can still judge "does this span say this?" -
so quality control does not require clinicians we do not have. It also gives the LLM
extractor a structural defence against hallucination (D3).

**Cost.** Real facts stated across two sentences, or implied rather than written, are
lost. We accept lower recall for auditable precision.

---

## D3. The LLM returns quotes; we assign the ontology IDs

**Decision.** The extraction tool schema has no field for an HPO/MONDO/HGNC identifier.
The model returns the verbatim phrase, whether it was negated, and which individual it
belongs to. Grounding happens in `rdcd/ontology/grounding.py`.

**Why.** A model asked for an identifier will sometimes return a well-formed, plausible,
wrong one, and no downstream check can detect it. A quote is falsifiable: it either
appears in the document or it does not. This single constraint buys hallucination
rejection, provenance-or-null compliance, and a legible failure mode all at once.

**Cost.** The grounder becomes a bottleneck - a correctly-extracted phrase that the
dictionary cannot ground is discarded (`quote_ungroundable` in the run stats). Measured
before blaming the model.

---

## D4. Two eval tracks, never averaged together

**Decision.** Track SINGLE = papers contributing exactly one gold case (261 papers).
Track PAPER = all papers, gold pooled per document (385). Every number states its track.

**Why.** The gold set is mostly multi-individual cohort papers (median 2 cases/paper, max
462). "Case-level F1" over those is ambiguous: comparing per individual needs individual
segmentation first, and a segmentation error would be charged to the phenotype extractor.
Track SINGLE removes segmentation from the measurement; Track PAPER measures corpus-scale
recall and is systematically kinder on recall, harsher on precision.

**Cost.** The cleanest track is only 261 papers. Small, but honest.

---

## D5. Dev/test split by hash of the PMID

**Decision.** `split_of(pmid)` hashes the PMID; ~50/50. Calibration on dev, test scored
once per reported release.

**Why.** Stable as the gold set grows - a paper cannot migrate between splits when
phenopacket-store adds cases. No shuffle seed to lose. Observed dev/test agreement on the
primary metric is 0.438 vs 0.443, so the split is not adversarial.

---

## D6. Score exact **and** ontology-aware F1; publish the primary metric by name

**Decision.** Primary metric is `observed_phenotype_graded_f1` - Lin similarity over the
HPO DAG, best-match on both sides. Exact-match F1 is reported alongside, never as a
substitute.

**Why.** Predicting `HP:0007359` "Focal-onset seizure" where the curator wrote
`HP:0001250` "Seizure" is a good extraction. Exact match scores it zero, which would
drive the extractor toward copying curator vocabulary rather than reading the paper.

**Cost.** Graded metrics are easier to game, which forced D7.

---

## D7. Threshold the graded similarity at 0.5

**Decision.** Partial credit below Lin similarity 0.5 is discarded.

**Why (measured).** Unrelated HPO terms still share high-level ancestors:
`lin(Seizure, Microcephaly) = 0.258`. Ungated, a wholly wrong prediction earns ~0.26 and
an F1 ≥ 0.85 target becomes partly reachable by coincidence. With the threshold, a
deliberately unrelated prediction scores exactly 0.000
(`tests/test_metrics.py::test_threshold_removes_the_free_credit_floor`).

---

## D8. Bootstrap confidence intervals on every headline number

**Decision.** 1,000-resample percentile bootstrap over cases, fixed seed.

**Why.** A bare F1 invites over-reading a difference that is noise. The dev/test gap of
0.005 is inside a CI half-width of ~0.036 - so it is nothing, and the interval says so
without argument.

---

## D9. Absent phenotypes are modelled and scored separately

**Decision.** `PhenotypeAssertion.excluded` mirrors the phenopacket field; observed and
excluded are scored as separate populations.

**Why.** 59.1% of gold features are `excluded: true`. Pooling lets an extractor that
ignores negation score well by flooding output with present findings, and makes one that
*inverts* negation look catastrophic for the wrong reason. Separate scoring exposes the
baseline's genuine weakness here (absent F1 0.11 vs observed 0.56).

---

## D10. Multi-word BROAD/RELATED synonyms are trusted; single-word ones are not

**Decision.** Grounding uses EXACT + NARROW synonyms, plus BROAD/RELATED synonyms that
contain a space.

**Why.** HPO scopes "Hearing loss" as RELATED to `HP:0000365`. Excluding all RELATED
synonyms loses one of the commonest phrases in the corpus; including single-word ones is
where false positives live. Multi-word phrases are far less ambiguous.

**What the ablation actually showed - no support.** Removing multi-word BROAD/RELATED
synonyms moves Track SINGLE graded F1 from 0.5581 to 0.5582: **−0.0001**. Not a small
gain - no gain at all. It adds 1,952 phrases for nothing measurable. The decision stands
only on the qualitative argument (a corpus-wide extractor that cannot read "hearing loss"
is deficient on its face) and could be reverted without cost. Recorded as **unsupported by
measurement**, because an ablation that fails to confirm a choice is the reason to run it.

---

## D11. Restrict extraction to patient-describing sections

**Decision.** Skip Introduction, Background, Discussion, Methods, and similar; keep title,
abstract, tables, and case-description body sections.

**Why.** Papers discuss *other* papers' patients at length. Phenotypes named while
reviewing prior literature are the largest single source of dictionary false positives.
Ablation quantifies the effect (BENCHMARKS).

**Cost.** Case details that appear only in a Discussion are lost.

---

## D12. Rank genes and diseases; do not emit every candidate

**Decision.** Score gene mentions by position (title ≫ abstract > body) and HGVS
co-occurrence; keep top-1 by default.

**Why (measured).** Recall for "the gold gene appears somewhere in the paper" is 68/70
(97%); precision was the entire problem at mean 10.9 genes per paper. On the 120-paper dev
subset used at the time, ranking moved gene F1 from **0.17 to 0.89** without touching
recall; on the full eval set the current figure is 0.856 (Track SINGLE) / 0.904 (Track
PAPER). A baseline this weak on genes would have been a strawman that flattered any
successor.

---

## D13. Tables are first-class text

**Decision.** `parse_jats` extracts `table-wrap` content as `Section.TABLE` and splits it
line-wise rather than sentence-wise.

**Why.** Multi-individual cohort papers put the per-patient phenotype grid in a table.
Dropping tables would discard exactly the data the gold set was curated from.

---

## D14. OBO over the JSON ontology releases

**Decision.** Parse `hp.obo` (11 MB) and `mondo.obo` (51 MB), not `hp.json` (23 MB).
Cache parsed ontologies as pickles keyed on source mtime+size.

**Why.** Smaller, and every field we need is present. Parse cost is ~1.2s cold, ~0.1s cached.

**Cost.** A hand-written parser, which produced a real bug: MONDO writes
`xref: OMIM:162200 {source="MONDO:equivalentTo"}`, and keeping the trailing modifier broke
every OMIM lookup silently. Now a regression test
(`tests/test_ontology.py::test_obo_modifiers_do_not_leak_into_xrefs`). The modifier turned
out to be useful - it distinguishes exact equivalence from broader matches.

---

## D15. Normalise all disease identifiers to MONDO

**Decision.** Gold uses OMIM; HPO annotations use OMIM and Orphanet; MONDO is the common
denominator.

**Why.** Without it, a correct answer in the wrong vocabulary scores zero. 98.8% of gold
diagnosis assertions normalise, so the residual 1.2% is a known ceiling, not a mystery.

---

## D16. All network I/O through one cached, rate-limited client

**Decision.** Every fetch goes through `rdcd/corpus/ncbi.py`, with a content-addressed
disk cache and a 2.8 req/s limiter (9/s with `NCBI_API_KEY`).

**Why.** NCBI's rate limits get honoured in one place, and - more importantly - scoring
becomes reproducible. `make eval` reads the cache, so a number cannot change because NCBI
served something different this afternoon. Fetching is a separate, explicit target.

---

## D17. `requests`, not `urllib`

**Decision.** All HTTP through a pooled `requests` session.

**Why.** This build environment's python.org install has no CA store for `urllib` (the
`Install Certificates.command` case) and every TLS handshake failed. `requests` ships
certifi. Chosen for portability, not preference.

---

## D18. Flag violations; never delete records

**Decision.** `qa/constraints.py` attaches flags and lowers `confidence`. Nothing is
dropped except unprovenanced assertions (D2).

**Why.** A silently removed record is indistinguishable, from the outside, from one we
never had. Severity is honest about the difference between impossible (a term present and
absent; a child present while its parent is absent) and merely improbable (a gene not
previously linked to the diagnosis - which is how new associations look).

---

## D19. Three independent retraction sources

**Decision.** Retraction Watch (via Crossref), PubMed `Retracted Publication[pt]`, and the
PMC OA service's `retracted` attribute.

**Why.** None is complete. The gold-set retraction was caught by the PMC attribute during
the licence audit and independently confirmed by Retraction Watch with a reason.

---

## D20. Batch API is load-bearing; prompt caching, measured, is not

**Decision.** The corpus-scale path is the Batch API. The extraction system prompt is
byte-stable and carries a `cache_control` breakpoint.

**Why the Batch API matters.** A corpus pass is offline, so up-to-24h turnaround costs
nothing and halves the per-token price. Measured on the 129-paper dev split: **$13.57
synchronous vs $6.78 batched.** That is the single biggest cost lever.

**Why caching turned out not to matter - corrected after measuring.** This decision
originally claimed caching was load-bearing too. It is not, at this prompt size. The system
prompt is ~585 tokens against ~770,000 tokens of document text, so caching it saves
**$0.34 of a $13.57 run - 2.5%.** Documents are unique per call and cannot be cached, which
is where all the input cost lives. Caching is kept because it is free to keep and would
matter if the prompt grew (few-shot examples, a long HPO style guide), but it is an
optimisation, not a design pillar. Claiming otherwise was an unmeasured assumption.

**A real trap it left behind.** The minimum cacheable prefix is model-dependent: 512 tokens
on Claude Opus 5, but **1024 on Opus 4.8 and most others**. At 585 tokens this prompt caches
on Opus 5 and would **silently stop caching** on a model with a 1024 minimum - no error,
just `cache_read_input_tokens: 0`. The runner prints cache reads for exactly this reason.
Anyone growing or shrinking `SYSTEM_PROMPT`, or changing model, should check that number.

The byte-stability requirement stands regardless: no per-paper content is ever interpolated
into the system prompt
(`tests/test_llm_extractor.py::test_system_prompt_is_cached_and_paper_independent`).


---

## D21. Ground phenotypes only under HP:0000118 (Phenotypic abnormality)

**Decision.** The HPO grounder keeps only terms descended from `HP:0000118`. HPO's other
root branches are excluded: Mode of inheritance (`HP:0000005`), Clinical modifier
(`HP:0012823`), Frequency (`HP:0040279`), Past medical history (`HP:0032443`), Blood group,
Clinical relevance.

**Why (measured).** Those branches contain real HPO terms that are not phenotypes -
"Affected", "Unaffected", "Healthy", "Left", "Right", "Bilateral", "Peripheral",
"Recurrent", "Family history", "Autosomal dominant inheritance". A phenopacket's
`phenotypicFeatures` never contains them: **100.00% of the 90,549 gold phenotype terms in
the eval set are `HP:0000118` descendants, and zero are outside it.** Before the fix, only
68.13% of predicted assertions were - so **31.9% of every phenotype assertion the extractor
produced was a guaranteed false positive.**

**Effect.** The largest single quality change in the project:

| | Before | After | Δ |
|---|---|---|---|
| Track SINGLE graded F1 | 0.4404 | **0.5581** | **+0.1177** |
| Track SINGLE exact F1 | 0.3162 | 0.4055 | +0.0893 |
| Track PAPER graded F1 | 0.5001 | 0.6064 | +0.1063 |
| Assertions in the release | 26,036 | 18,459 | −29% |
| Polarity-contradiction ERRORs | 1,045 | 639 | −39% |

Gene and diagnosis metrics are unchanged, as expected - the filter applies to HPO only, and
a test pins that it does not leak into MONDO or gene grounding.

**It propagated downstream.** Re-running the diagnostic benchmark moved pipeline top-1 recall
0.140 → 0.229 and MRR 0.231 → 0.345, while the ceiling row stayed byte-identical (it uses
gold phenotypes, so it must). The intrinsic gain was real, not metric-gaming.

**How it was found.** Not by the metric, which had been reporting 0.44 for hours without
complaint, and not by a test. It was found by rendering the reference UI and *looking at*
the phenotype chips on real records, where "Affected" and "Autosomal dominant inheritance"
are obviously not phenotypes. The lesson worth keeping: an aggregate score cannot tell you
that the units it is aggregating are the wrong kind of thing. Build the thin UI early, and
read its output - it is a debugging instrument before it is a product.

**Cost.** None identified. Reproducible as an ablation:
`scripts/run_eval.py --no-phenotype-root`.
