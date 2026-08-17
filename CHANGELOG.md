# Changelog

## 0.1.0-dev - 2026-08-17

First build. Month-1 foundation: eval harness, schema, ontology layer, baseline extractor,
QA suite, diagnostic benchmark, release builder, read API + UI. 47 offline tests.

### Measured

- Eval set: 646 papers / 4,147 gold cases (from 10,377; the rest are not legally readable).
- Dictionary baseline, Track SINGLE test: phenotype graded F1 **0.557**, exact 0.401,
  absent 0.108, gene 0.826, diagnosis 0.246.
- Provenance: 99.43% of 32,173 cited spans verifiably support their assertion.
- Diagnostic ranking: extraction error costs **29.5 points of top-1 recall**
  (ceiling 0.523 → pipeline 0.229).
- Release: 646 records, 18,459 assertions, with datasheet and SHA256SUMS.

### Fixed during the build

- **Phenotype grounding restricted to `HP:0000118`**: 31.9% of assertions were HPO
  modifier/inheritance/frequency terms that phenopackets never use. Track SINGLE graded F1
  0.4404 → 0.5581 (+0.118); pipeline top-1 0.140 → 0.229. Found by rendering the UI, not by
  the metric or the tests. (D21 / E10)
- **OBO xref modifiers leaking into CURIEs**: MONDO writes
  `xref: OMIM:162200 {source="MONDO:equivalentTo"}`; keeping the brace block silently broke
  every OMIM→MONDO lookup. (D14)
- **Disease abbreviations resolving to genes**: `LCA` → `GUCY2D`, `KS` → `OXSM` via HGNC
  aliases. (E6)
- **Gene anchored only on the approved symbol**: a paper writing `FOG2` for `ZFPM2` yielded
  a correct gene that was then dropped. Now tries every HGNC alias. (E7)
- **Negation scope leaking across sentence boundaries.**
- **ISO-8601 week durations** (`P32W`, gestational ages) silently unparsed, skipping the
  onset-consistency check.
- **Graded P/R identity broken under micro-averaging**: graded numerators now carried
  separately so summed PRFs reproduce precision and recall exactly.
- **Term-index double-counting** in the API when a case asserted the same term both present
  and absent.

### Added after first push

- Abstract-only vs full-text experiment (`--abstract-only`, `--tier`), quantifying what the
  `abstract_only` licence tier costs: -46% phenotype F1, -92% absent-finding F1, gene and
  diagnosis unchanged.
- `make llm-dev` / `llm-dev-dry` / `llm-dev-batch`: the go/no-go run, with cost estimate
  ($13.57 sync, $6.78 batched) and `.env` support so no key touches shell history.
- `make diagnostic-abstract`: the same ceiling/pipeline benchmark on abstract-only input.
  Result: abstract-only keeps 64% of full-text top-20 recall but only 36% of top-1, so the
  tier shortlists rather than diagnoses; 10% of its papers yield no phenotypes at all.
- Apache-2.0 `LICENSE` and `LICENSE-DATA`; personal email replaced with a placeholder and a
  warning; `FILES.md` + a self-checking manifest generator; em dashes removed from prose.

### Measured (pilot)

- First LLM extraction, without an API key: document text written to disk, read by an
  assistant with filesystem access, scored through the same parser as the API path
  (`make manual-prep` / `make manual-score`). n=7, first papers by PMID.
  **0% hallucination**, provenance 1.000, graded F1 0.712 vs the 0.560 dictionary floor,
  but exact F1 unchanged (0.415 vs 0.411) and absent-finding F1 *worse* (0.059 vs 0.119).

### Added

- `label` field on findings: the extractor supplies a normalised clinical term alongside the
  verbatim quote, and the label is what gets grounded. Grounder loss fell from **46.8% to
  2.2%** and absent findings became recoverable at all. No identifier ever originates with
  the model; an ungroundable label is still dropped.
- `grounded_from` on `PhenotypeAssertion`, so the provenance verifier re-checks the same
  derivation it used rather than re-grounding the raw span.
- Unconditional top-k metrics and `coverage` on the diagnostic benchmark, alongside the
  conventional conditional figures.

### Corrected

- **The provenance verifier reported 44% of sound assertions as unsupported** once labels
  were introduced, because it re-grounded the span instead of the derivation. Support rate
  0.5645 was an artifact; it is 1.000 with the fix, and the dictionary path is unaffected.
- **D20 overstated prompt caching.** Measured: it saves $0.34 of $13.57 (2.5%), not a design
  pillar. The Batch API is the real lever. Also documented the 512-vs-1024 cache-minimum
  trap that would silently disable caching on a model change.

### Known gaps

- LLM extractor has never made a live API call; its accuracy is unmeasured.
- No corpus-scale extraction pass.
- `docs/REQUIREMENTS.md` §2 literature synthesis not done - sources named, no findings
  invented.
- Ontology versions not pinned (the gold set is).
