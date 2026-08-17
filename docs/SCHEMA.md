# Schema v1 (`SCHEMA_VERSION = "1.0.0"`)

Two commitments: every record exports to a GA4GH Phenopacket v2 without losing the fields
Phenopackets models, and every asserted fact carries provenance. Defined in
`rdcd/schema.py`; round-trip tested in `tests/test_schema.py`.

## Why phenopacket-compatible

The gold set *is* phenopackets. Sharing the shape means prediction and gold are directly
comparable, and downstream tools that already read phenopackets (Exomiser, LIRICAL) can
read our releases without an adapter. `CaseRecord.to_phenopacket()` and
`.from_phenopacket()` are inverses over the scored fields.

## CaseRecord

One individual, extracted from one document.

| Field | Type | Notes |
|---|---|---|
| `id` | str | Stable within a release |
| `schema_version` | str | `"1.0.0"` |
| `source` | `SourceDoc` | Identifiers, licence, retraction status |
| `subject` | `Subject` | Sex, age, vital status |
| `phenotypes` | `list[PhenotypeAssertion]` | Present **and** explicitly absent |
| `diagnoses` | `list[DiagnosisAssertion]` | |
| `variants` | `list[VariantAssertion]` | |
| `confidence` | `float \| None` | Written by QA only, never by an extractor |
| `qa_flags` | `list[str]` | e.g. `error:dag_polarity_contradiction` |
| `extractors` | `list[str]` | Which extractors contributed |

Convenience views used by the harness: `observed_hpo`, `excluded_hpo`, `gene_symbols`,
`gene_ids`, `disease_ids`.

Provenance API: `unprovenanced()` lists field paths asserting something without evidence;
`enforce_provenance()` returns a cleaned copy plus what was dropped.

## Evidence — the load-bearing type

| Field | Type | Notes |
|---|---|---|
| `source_id` | str | CURIE, e.g. `PMID:36996813` |
| `section` | `Section` | title / abstract / body / table / figure_caption / supplement |
| `start`, `end` | `int \| None` | Character offsets into the normalised document text |
| `quote` | `str \| None` | **Only** when the source licence permits redistributing expression |
| `extractor` | `str \| None` | Which extractor produced this |

`locator` renders as `PMID:36996813#abstract:79-110`.

Offsets index the text produced by `rdcd/corpus/jats.py`, which is deterministic: the same
JATS in gives the same offsets out, permanently. That determinism is what makes an offset a
citation rather than a hint.

## PhenotypeAssertion

| Field | Notes |
|---|---|
| `term` | `OntologyClass` — HPO |
| `excluded` | `True` = the source states it was looked for and **not** found |
| `onset` | `TimeElement` |
| `severity` | `OntologyClass` |
| `negation_cue` | The word that carried the negation, kept for audit |

`excluded` is not "unknown" and not "low confidence" — it is a positive assertion of
absence, and it is 59.1% of the gold data.

## Other types

- **`OntologyClass`** — `{id, label}`; `id` must be a CURIE (validated).
- **`TimeElement`** — mirrors the Phenopacket one-of in the forms case reports use:
  ISO-8601 duration, gestational weeks/days, HPO onset class, or an age range.
- **`Subject`** — `id`, `sex`, `age_at_last_encounter`, `vital_status`.
- **`DiagnosisAssertion`** — `disease`, `status`, and `stated_in_abstract` (marks the
  distant-supervision population).
- **`VariantAssertion`** — `gene` (HGNC), `hgvs_c` / `hgvs_p` / `hgvs_g`, `allelic_state`
  (GENO), `acmg`.
- **`SourceDoc`** — `pmid`, `pmcid`, `doi`, bibliographic fields, plus `license`,
  `in_oa_subset`, `quotes_permitted`, `retracted`, `retraction_notice`.

## Compatibility policy

Additive changes bump the minor version; any change to the meaning of an existing field
bumps the major version and is listed in the release notes. `schema_version` is stored on
every record so a consumer never has to guess.
