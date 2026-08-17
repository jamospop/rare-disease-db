# Handover - what was built, what was found, what to do next

A complete record of the month-1 build, written so someone picking this up cold does not
have to reconstruct the reasoning. Findings are stated with the numbers that produced them
and the script that produced the numbers.

Built 2026-08-17. Inputs: phenopacket-store 0.1.27, ontologies fetched the same day.

---

## 1. What exists

| Component | File | State |
|---|---|---|
| Schema v1, phenopacket-compatible, provenance-carrying | `rdcd/schema.py` | Working, round-trip tested |
| Rate-limited, disk-cached NCBI/PMC client | `rdcd/corpus/ncbi.py` | Working |
| JATS → sectioned text with stable offsets | `rdcd/corpus/jats.py` | Working, offsets verified exact |
| OBO reader with pickle cache | `rdcd/ontology/obo.py` | Working |
| HPO/MONDO/HGNC/HPOA store, IC, gene-disease links | `rdcd/ontology/store.py` | Working |
| Dictionary grounding + NegEx negation | `rdcd/ontology/grounding.py` | Working |
| Dictionary baseline extractor | `rdcd/extract/baseline.py` | Working, measured |
| LLM extractor (quotes in, ontology IDs out) | `rdcd/extract/llm.py` | **Never executed live** |
| Gold loading, licence audit, tracks/splits | `rdcd/eval/{goldsets,evalset}.py` | Working |
| Metrics: exact + ontology-aware, bootstrap CIs | `rdcd/eval/metrics.py` | Working |
| Offline scoring harness | `rdcd/eval/harness.py` | Working |
| Phenotype → disease ranker | `rdcd/eval/diagnose.py` | Working |
| QA: provenance, constraints, consensus, distant, retractions | `rdcd/qa/*.py` | Working |
| Versioned release builder | `scripts/build_release.py` | Working |
| Read API + reference UI | `scripts/serve_api.py` | Working |

47 offline tests. `make reproduce` regenerates every published number with no API key.

**Not built:** corpus-scale extraction pass, Postgres/pgvector, nightly ingestion daemon,
Hugging Face / Zenodo publication, preprint.

---

## 2. Findings, in order of consequence

### F1. Conservative licensing costs 60% of the gold eval set
`reports/goldset_availability.json` - audit of all 1,733 gold source papers.

| Tier | Papers | Gold cases |
|---|---|---|
| `full_text_quotable` (CC BY / CC0) | 442 | 3,002 |
| `full_text_facts_only` (NC / ND / SA) | 205 | 1,147 |
| `abstract_only` (not in OA subset) | 1,086 | 6,228 |

1,023 of 1,733 are in PMC; only **647 are in the OA subset**; only 442 permit quoted
evidence. The scoreable eval set is **4,147 cases, not 10,377**.

**Consequence:** every coverage claim must be stated against 647 readable papers, not
PubMed's 2,572,683 `case reports[pt]`. This ceiling is unaffected by any extraction
improvement. If the project needs full-text scale beyond the OA subset, that is a *legal*
work item (TDM exceptions), not an engineering one.

### F2. Extraction, not ranking, is the bottleneck - by 29.5 points of top-1 recall
`reports/diagnostic_benchmark_single.json`, 258 papers, Track SINGLE.

| Condition | top-1 | top-3 | top-10 | top-20 | MRR |
|---|---|---|---|---|---|
| Ceiling (gold phenotypes) | 0.523 | 0.628 | 0.733 | 0.798 | 0.599 |
| Pipeline (extracted) | 0.229 | 0.411 | 0.566 | 0.659 | 0.345 |
| **Cost of extraction error** | **−0.295** | −0.217 | −0.167 | −0.140 | −0.254 |

**Consequence:** spend the next month on extraction. Also bounds the ranker honestly -
ceiling top-20 of 0.798 means even perfect extraction leaves 20% of cases unranked by this
method, so a better ranker is eventually needed, just not first.

### F3. Nearly a third of phenotype assertions were the wrong *kind* of thing
The largest quality bug found, and the most instructive.

HPO contains real terms that are not phenotypic abnormalities: `Affected`, `Unaffected`,
`Healthy`, `Left`, `Right`, `Bilateral`, `Peripheral`, `Recurrent`, `Family history`,
`Frequency`, `Autosomal dominant inheritance`. A phenopacket's `phenotypicFeatures` never
contains them.

- **100.00%** of the 90,549 gold phenotype terms are `HP:0000118` descendants; **zero** are outside.
- Only **68.13%** of predictions were → **31.9% of every phenotype assertion was a guaranteed false positive.**

Restricting the grounder to `HP:0000118`:

| | Before | After | Δ |
|---|---|---|---|
| Track SINGLE graded F1 | 0.4404 | **0.5581** | **+0.1177** |
| Track SINGLE exact F1 | 0.3162 | 0.4055 | +0.0893 |
| Pipeline top-1 (diagnostic) | 0.140 | 0.229 | +0.089 |
| Pipeline MRR | 0.231 | 0.345 | +0.114 |
| Polarity-contradiction ERRORs | 1,045 | 639 | −39% |
| Release assertions | 26,036 | 18,459 | −29% |

**How it was found: by rendering the UI and looking at it.** Not by the primary metric,
which reported 0.44 for hours without complaint. Not by the 45 tests then passing. The
before/after screenshots are in `docs/img/`. An aggregate score cannot tell you that the
units it aggregates are the wrong kind of thing - see `docs/REQUIREMENTS.md` R11.

The diagnostic benchmark, run before and after, produced a **byte-identical ceiling row**
while only the pipeline row moved - which is what makes the +0.089 attributable to the fix
rather than to noise.

### F4. The expert gold data contains errors the checker catches
`reports/qa_audit.json`, 4,149 gold cases.

| Code | Severity | Count |
|---|---|---|
| `obligate_phenotype_excluded` | WARN | 962 |
| `onset_after_last_encounter` | ERROR | 77 |
| `unmappable_disease` | WARN | 49 |
| `outdated_hpo_term` | WARN | 10 |

The 77 onset errors are genuine (encounter `P18Y` with onset `P19Y`; `P1D` with `P1M21D`),
spot-checked against raw records. **Worth reporting upstream to Monarch.** Zero polarity
contradictions in gold - the curators are internally consistent, which is the strongest
available validation of the checker's ERROR tier.

### F5. A retracted paper sits inside the expert gold set
PMID 30850397 - image duplication, IRB failure. Caught by the PMC OA `retracted` attribute
during the licence audit, independently confirmed by Retraction Watch (94,265 notices
indexed). Excluded from the eval set by default; flagged, never deleted.

### F6. Absent findings are 59% of the target and our worst dimension
92,068 observed vs 132,795 excluded features across 10,377 gold cases. Baseline
absent-phenotype F1 is **0.108** against 0.557 for present findings.

**The project plan sets no target for this.** The 0.85 phenotype target can be met while
being wrong about the majority of the data. This is a gap in the plan, not only the code.

### F7. The abstract-only tier costs half the phenotypes and all the negation
`reports/eval_tier_q_fulltext.json` vs `reports/eval_tier_q_abstract.json`. Same 441
quotable-tier papers, extracted twice: full text vs title+abstract only.

| | Full text | Abstract only | Change |
|---|---|---|---|
| Phenotype graded F1 (SINGLE) | 0.5510 | 0.2981 | -46% |
| Absent-finding F1 | 0.1182 | 0.0089 | **-92%** |
| Gene F1 | 0.8373 | 0.8528 | +2% |
| Diagnosis F1 | 0.2553 | 0.2553 | 0% |

**Consequence:** abstract-tier extraction is worth shipping for the 1,086 papers we cannot
read in full - gene and diagnosis survive intact, phenotypes retain about half - but records
from that tier must be flagged as having no negative-finding data. F1 0.0089 is absence, not
weakness: abstracts report what was found, never what was ruled out. Publishing those records
unflagged would let a consumer read "absent from the record" as "reported absent".

Full-text access to that tier (a legal question, see `docs/LICENSING.md`) would roughly
double usable coverage: ~4,100 to ~10,400 gold cases.

### F8. Two design choices the measurements did *not* support
Recorded because an ablation that fails to confirm a choice is the reason to run it.

- **Multi-word BROAD/RELATED synonyms** (D10): +1,952 phrases for **−0.0001** F1. No
  measurable benefit. Kept on the qualitative argument only; could be reverted at no cost.
- **Patient-section filter** (D11): **+0.0383** F1, intervals barely overlapping. Genuinely
  earns its place.

### F9. Prompt caching is not worth what I claimed
Measured on the 129-paper dev run: caching the shared system prompt saves **$0.34 of $13.57
(2.5%)**. The system prompt is ~585 tokens against ~770,000 tokens of document text, and
documents are unique per call so cannot be cached. The Batch API is the real lever
($13.57 -> $6.78). D20 originally called caching load-bearing; that was an unmeasured
assumption and is now corrected.

**A trap it leaves:** the minimum cacheable prefix is 512 tokens on Claude Opus 5 but 1024 on
most other models. At 585 tokens this prompt caches on Opus 5 and would **silently stop**
caching on a model with a 1024 minimum - no error, just `cache_read_input_tokens: 0`. The
runner prints that number for this reason.

### F10. Bugs found by the QA layer rather than by tests
The QA suite caught real extractor defects, which is the mechanism working as designed:

- Disease abbreviations resolving to genes via HGNC aliases - `LCA` (Leber congenital
  amaurosis) → `GUCY2D`, `KS` (Kabuki/Kallmann) → `OXSM`. Fixed with a blocklist.
- Gene anchored only on the approved symbol, so a paper writing `FOG2` for `ZFPM2` produced
  a correct gene that was then silently dropped. Fixed to try every HGNC alias and to quote
  the spelling the paper actually uses.
- My own OBO parser kept MONDO's trailing `{source="MONDO:equivalentTo"}` modifier inside
  the CURIE, silently breaking **every** OMIM→MONDO lookup. Now a regression test.

---

## 3. Current measured state

Dictionary baseline, 646 papers. Primary metric `observed_phenotype_graded_f1`.

| Group | n | Exact F1 | Graded F1 | 95% CI | Absent F1 | Gene F1 | Dx F1 |
|---|---|---|---|---|---|---|---|
| single / test | 132 | 0.401 | **0.557** | [0.511, 0.602] | 0.108 | 0.826 | 0.246 |
| single / dev | 129 | 0.411 | 0.560 | [0.530, 0.592] | 0.119 | 0.887 | 0.245 |
| paper / all | 385 | 0.440 | 0.606 | [0.581, 0.631] | 0.068 | 0.904 | 0.120 |

Provenance: 99.43% of 32,173 cited spans verifiably support their assertion (0.57% fail, all
negation-scope disagreements). Note this is near-tautological for a dictionary extractor -
its value is as a floor and as the number that becomes a real hallucination rate under an LLM.

Against the plan's targets: gene is within 0.024; phenotypes are −0.293; diagnosis −0.604.

---

## 4. Next actions, in priority order

1. **Run the LLM extractor once, live. This is now one command.**

   ```bash
   echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env     # .env is gitignored
   make llm-dev-dry     # cost estimate, no API calls
   make llm-dev         # 129 dev papers, ~$13.57
   make llm-dev-batch   # same via Batch API, ~$6.78, up to 24h
   ```

   Everything else in this repository is scaffolding around an unmeasured centrepiece; this
   run converts it into a result. It prints `quote_not_found` (the model's hallucination
   rate) and `quote_ungroundable` (our grounder's recall loss) **before** any F1, because a
   low F1 with high `quote_ungroundable` is a grounder problem, not a model problem. Compare
   against the dictionary baseline's Track SINGLE dev 0.5597 and the plan's 0.85 target.

   Then re-run `make diagnostic` and watch whether the pipeline row moves toward the ceiling.
   The observed exchange rate from the D21 fix was +0.089 top-1 per +0.118 phenotype F1.
2. **Answer the existential question in `docs/REQUIREMENTS.md` §2 Q1.** PMC-Patients (167k)
   and RareArena (70k) already publish structured case collections. Read what they contain.
   If they already provide per-field provenance, phenopacket-native output, and licence
   tiering, **this project should stop.** Nobody has checked. This is cheap and it gates
   everything else.
3. **Attack absent-findings extraction** (F1 0.108, 59% of the target). Tabular negation
   notation and scope-over-conjunction are the two known failure modes.
4. **Individual segmentation.** 1,262 polarity-contradiction ERRORs are the measured cost of
   document-level records. This is the LLM extractor's main job.
5. **Diagnosis extraction** (F1 0.246, ceiling 0.988). Least developed component.
6. **Pin ontology versions.** The gold set is pinned; HPO/MONDO/HGNC are not, so an old
   number cannot be reproduced exactly. Main remaining reproducibility gap.
7. Report F4's 77 onset errors upstream to Monarch.

---

## 5. Traps for whoever picks this up

- **`make eval` is cache-only.** Without `make fetch-fulltext` first, every paper is skipped
  rather than silently refetched. `make reproduce` chains them correctly.
- **Never compare across tracks.** Track PAPER scores ~0.05 higher than Track SINGLE by
  construction. Every quoted number must name its track.
- **The primary metric excludes absent findings.** 0.557 is not "how good is extraction".
- **Graded F1 needs its threshold.** Without it, unrelated HPO terms score ~0.26 for free
  and the 0.85 target becomes partly reachable by coincidence.
- **Distant supervision is a regression detector, not a benchmark.** 633/646 agreement looks
  strong but only measures papers that state the answer in the abstract.
- **Don't add per-paper content to the LLM system prompt.** It is the cache prefix; a test
  pins its paper-independence. Interpolating anything there silently multiplies cost.
- **Nothing is committed to git.** The repo was `git init`-ed but has no commits, so
  `reports/*.json` carry `git_rev: "uncommitted"`.
