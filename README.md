# Open Rare-Disease Case Database

Structured, provenance-linked, individual-level case data mined from the rare-disease
literature — with a reproducible benchmark that measures how good the extraction
actually is.

Rare-disease diagnosis depends on published single cases and small cohorts, but that
evidence is locked in prose. Curated resources exist and are excellent (GA4GH
phenopacket-store, RAMEDIS, ClinVar); what is missing is *coverage* — a machine-readable
corpus spanning the whole published literature, where every field points back at the
sentence it came from.

**Status: month-1 foundation.** The eval harness, schema, ontology layer, QA suite,
diagnostic benchmark, and a no-API-key baseline extractor are built and measured. The
corpus-scale extraction pass has not been run. Every number below was produced by
`make reproduce` on this repository and can be regenerated from public sources.

---

## What is actually here

| Component | State |
|---|---|
| Phenopacket-compatible schema with per-field provenance (`rdcd/schema.py`) | Working, round-trip tested |
| Eval harness against 4,147 expert-curated gold cases (`rdcd/eval/`) | Working, dev/test split |
| Ontology layer: HPO, MONDO, HGNC, HPO annotations (`rdcd/ontology/`) | Working |
| Dictionary + negation baseline extractor (`rdcd/extract/baseline.py`) | Working, no API key needed |
| LLM extractor (`rdcd/extract/llm.py`) | Written, **not executed live** — see Limitations |
| QA: provenance verification, ontology constraints, consensus, retractions (`rdcd/qa/`) | Working |
| Phenotype-driven diagnostic ranker + ceiling/pipeline benchmark (`rdcd/eval/diagnose.py`) | Working |
| Corpus-scale extraction pass, public API, dataset release | Not built |

## Headline numbers

Dictionary baseline, 646 papers, HPO phenotype extraction. **Primary metric:
`observed_phenotype_graded_f1`** (ontology-aware F1 over present phenotypes;
see [BENCHMARKS](docs/BENCHMARKS.md) for why exact match alone is the wrong target).

| Track | n | Phenotype F1 (exact) | Phenotype F1 (graded) | 95% CI | Absent-phenotype F1 | Gene F1 | Diagnosis F1 |
|---|---|---|---|---|---|---|---|
| SINGLE / test | 132 | 0.401 | **0.557** | [0.511, 0.602] | 0.108 | 0.826 | 0.246 |
| SINGLE / dev | 129 | 0.411 | 0.560 | [0.530, 0.592] | 0.119 | 0.887 | 0.245 |
| PAPER / all | 385 | 0.440 | 0.606 | [0.581, 0.631] | 0.068 | 0.904 | 0.120 |

Read this as a **floor, not a result.** A dictionary with negation gets phenotype
graded F1 0.56 and gene F1 0.83; the project's target is 0.85. The gap is what an
LLM extractor has to earn, measured on the same split with the same metric.

Provenance audit over 32,173 baseline assertions: **99.43% of cited spans verifiably
support their assertion**; the 0.57% that fail are all negation-scope disagreements.

### What extraction error costs diagnosis

The same ranker, run twice over 258 papers — once on expert gold phenotypes (the ceiling),
once on extracted ones (the pipeline):

| Condition | top-1 | top-3 | top-10 | top-20 | MRR |
|---|---|---|---|---|---|
| Ceiling (gold phenotypes) | 0.523 | 0.628 | 0.733 | 0.798 | 0.599 |
| Pipeline (extracted) | 0.229 | 0.411 | 0.566 | 0.659 | 0.345 |
| **Cost of extraction error** | **−0.295** | −0.217 | −0.167 | −0.140 | −0.254 |

Given perfect phenotypes the ranker reaches 80% top-20, so the ranker is not the
bottleneck — extraction is, by 29.5 points of top-1 recall. That is the answer to "what
should the next month go into", and it is why this repository's effort is on extraction.

The benchmark also validates itself: run before and after the phenotype-branch fix, the
ceiling row is byte-identical while pipeline top-1 rose 0.140 → 0.229 and MRR 0.231 → 0.345.
A +0.118 phenotype-F1 gain bought +0.089 top-1 of real diagnostic improvement.

## The design commitments

**1. Provenance-or-null.** Every asserted fact carries at least one `Evidence` with a
character offset into the source document. A fact with no evidence is not a
low-confidence fact — it is dropped. This is what makes non-clinician audit possible:
checking "does this span say this?" needs reading comprehension, not a medical degree.
`rdcd/qa/provenance.py` re-reads every cited span and reports the support rate.

**2. The LLM never emits an ontology ID.** It returns the verbatim phrase, the polarity,
and the individual it belongs to; we ground phrases to HPO/MONDO/HGNC ourselves. A model
asked for `HP:0001250` will occasionally produce a plausible wrong identifier that nothing
downstream can catch. A quote either appears in the document or it does not — so
hallucinated findings are *structurally* rejected, not merely down-weighted. Verified in
`tests/test_llm_extractor.py`.

**3. Absent findings are first-class.** 59.1% of phenotype features in the expert gold
data are `excluded: true` — findings explicitly looked for and not found. A pipeline that
cannot represent negation is wrong about the majority of the target, and a metric that
pools present with absent hides it. We model and score them separately.

**4. Conservative-by-construction licensing.** Full text is read only from the PMC Open
Access subset. Character offsets and extracted facts are published for everything;
verbatim quotes only where the licence permits redistributing expression. See
[LICENSING](docs/LICENSING.md). This is expensive — it cuts the gold eval set from
10,377 cases to 4,147 — and it is not negotiable.

**5. Flag, never silently delete.** Retracted sources, constraint violations, and
low-agreement records ship with their flags attached. A wrong record you can see is worth
more than a missing record you cannot.

## The finding that shapes the project

Auditing what the licensing rule permits, across all 1,733 gold-set source papers:

| Tier | Papers | Gold cases | May we read full text? | May we quote it? |
|---|---|---|---|---|
| `full_text_quotable` (CC BY / CC0) | 442 | 3,002 | yes | yes |
| `full_text_facts_only` (CC BY-NC / ND / SA) | 205 | 1,147 | yes | no |
| `abstract_only` (not in the OA subset) | 1,086 | 6,228 | no | no |

Only **37% of gold-set papers are in the PMC OA subset at all**, and only 26% permit
quoted evidence. Any claim about corpus-scale coverage of the rare-disease literature has
to be stated against this ceiling, not against PubMed's 2.57M `case reports[pt]`. The
audit also flagged one retracted paper already inside the gold set (PMID 30850397),
independently confirmed by Retraction Watch.

## Quickstart

```bash
make install          # pydantic, requests, lxml, pytest
make data             # ~140 MB: HPO, MONDO, HGNC, HPO annotations, phenopacket-store
make test             # 47 tests, no network (41 skip until `make data` has run)
make audit            # licence + retraction status of every gold source  (~10 min, network)
make fetch-fulltext   # cache JATS full text for the eval set             (~10 min, network)
make eval             # score the baseline — offline, reads only the cache
make diagnostic       # ceiling vs pipeline diagnostic ranking
make qa               # provenance + constraint audit
```

No API key is required for any of the above. `make reproduce` runs the scoring targets
end to end. Fetching and scoring are separate on purpose: after the cache is warm,
scoring touches no network and cannot drift because a source changed.

## Repository layout

```
rdcd/
  schema.py            Schema v1: phenopacket-compatible, provenance-carrying
  corpus/ncbi.py       Rate-limited, disk-cached NCBI/PMC client (all network I/O)
  corpus/jats.py       JATS XML -> sectioned text with stable character offsets
  ontology/obo.py      Compact OBO reader with a pickle cache
  ontology/store.py    HPO / MONDO / HGNC / HPOA, information content, gene-disease links
  ontology/grounding.py Dictionary grounding + NegEx-style negation
  extract/baseline.py  Dictionary baseline (the floor)
  extract/llm.py       LLM extractor: quotes in, ontology IDs out; Batch API + caching
  eval/goldsets.py     Gold loading + licence availability audit
  eval/evalset.py      Track/split construction
  eval/metrics.py      Exact + ontology-aware scoring, bootstrap CIs
  eval/harness.py      Offline scoring runs
  eval/diagnose.py     Phenotype -> disease ranker for the lift measurement
  qa/                  provenance, constraints, consensus, distant supervision, retractions
docs/                  Handover, decisions, licensing, benchmarks, error ledger, schema
docs/img/              Before/after evidence for the phenotype-branch fix
reports/               Generated evidence for every number quoted in docs/ (tracked)
scripts/               Runnable entry points (all wired into the Makefile)
tests/                 47 offline tests
```

## Documentation

- [FILES](FILES.md) — annotated inventory of every file in the repository
- [HANDOVER](docs/HANDOVER.md) — **start here**: what was built, every finding with its
  numbers, next actions in priority order, and the traps
- [DECISIONS](docs/DECISIONS.md) — every non-obvious design choice and what it cost
- [BENCHMARKS](docs/BENCHMARKS.md) — metric definitions, full results, ablations
- [ERROR_LEDGER](docs/ERROR_LEDGER.md) — known error classes with measured rates
- [LICENSING](docs/LICENSING.md) — what we read, what we publish, why
- [SCHEMA](docs/SCHEMA.md) — schema v1 field reference
- [REQUIREMENTS](docs/REQUIREMENTS.md) — what to build and why, plus what is still unverified
- [REPRODUCE](docs/REPRODUCE.md) — regenerating every published number
- [CHANGELOG](CHANGELOG.md) — what was measured, and every bug fixed during the build

## Limitations, stated plainly

- **The LLM extractor has never made a live API call.** No API key existed in the build
  environment. Request construction, response parsing, grounding, and hallucination
  rejection are unit-tested offline; the network path is not. Its accuracy is therefore
  **unmeasured** — the only measured extractor here is the dictionary baseline.
- **The baseline does not segment individuals.** It emits one record per document, which
  is why Track SINGLE (one gold case per paper) is the honest comparison and why the
  QA audit reports 1,262 polarity contradictions at paper level: the same term is present
  for one individual and absent for another. Per-individual segmentation is the LLM
  extractor's main job.
- **No corpus-scale run.** All numbers are on the 646-paper gold eval set.
- **The requirements document is not yet grounded in the usability literature.** It states
  what this repository measured; the published-workflow synthesis the project plan calls
  for still needs the source papers read. Sources are named, findings are not invented.
- **Diagnosis extraction is weak** (F1 0.25). Disease-name grounding via MONDO labels is
  the least developed part of the baseline.
- **Absent-findings recall is poor** (F1 0.11) even though absent findings are 59% of the
  target. The headline phenotype F1 does not cover this; do not read 0.56 as "how good is
  extraction".
- 99.8% of the HPO annotation file's `sex` field is empty, so sex-linked constraint
  checks run off inheritance-mode annotations instead — a weaker signal than intended.

## Licence

Code: Apache-2.0 ([`LICENSE`](LICENSE)). Extracted data: CC BY 4.0
([`LICENSE-DATA`](LICENSE-DATA)), with per-record source attribution and licence tier.
Neither covers the underlying publications — see [LICENSING](docs/LICENSING.md) for what we
read, what we publish, and why.

## Provenance of the inputs

HPO and MONDO (`purl.obolibrary.org`, CC BY 4.0) · HGNC (EBI, CC0) · HPO annotations
(`phenotype.hpoa`) · GA4GH phenopacket-store 0.1.27 (Monarch Initiative) · PubMed / PMC
E-utilities · PMC OA service · Retraction Watch via Crossref (CC BY 4.0). All are fetched
by `make data`; none are vendored.
