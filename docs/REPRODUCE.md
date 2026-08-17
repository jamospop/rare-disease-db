# Reproducing every published number

No API key, no account, no credentials. Two machine-dependent things: NCBI response times
and available disk.

## Requirements

- Python 3.11+ (built and tested on 3.13)
- ~1 GB disk: ~140 MB ontologies + gold set, ~250 MB HTTP cache, plus a 66 MB Retraction
  Watch export
- Network access to `purl.obolibrary.org`, `github.com`, `ncbi.nlm.nih.gov`, and
  `api.labs.crossref.org`

## The sequence

```bash
make install          # pydantic, requests, lxml, pytest
make data             # ontologies + phenopacket-store 0.1.27      (~140 MB)
make test             # 47 tests, no network                       (~5 s)
make audit            # licence + retraction status, 1,733 papers   (~10 min, rate-limited)
make fetch-fulltext   # cache JATS full text for 646 eval papers    (~10 min, rate-limited)
make eval             # score the baseline                          (offline)
make qa               # provenance + constraint audit                (offline)
make diagnostic       # ceiling vs pipeline ranking                  (offline, CPU-heavy: ~30 min)
```

`make test` needs no network but does need the ontologies: before `make data` it reports
`6 passed, 41 skipped` with the reason in the header, rather than failing. After `make data`
all 47 run.

`make reproduce` chains data → audit → fetch-fulltext → eval → ablations → qa → diagnostic,
so a fresh clone reaches every published number in one command. Set `NCBI_API_KEY` to raise the rate limit
from 2.8 to 9 requests/second and cut the network steps to roughly a third.

**Set `RDCD_EMAIL` before any sustained fetching.** NCBI's E-utilities terms require a
contact address on every request; the code warns if it is unset and sends a placeholder.

```bash
export RDCD_EMAIL=you@example.org
export NCBI_API_KEY=...            # optional, 3x faster
```

## Why fetching and scoring are separate

Scoring reads only the content-addressed cache under `data/cache/`. Once warm, `make eval`
touches no network, so a reported number cannot change because a source was updated between
runs. `make clean-cache` deliberately breaks reproducibility and exists only for a genuine
refresh.

## What each target writes

| Target | Output | Contains |
|---|---|---|
| `make audit` | `reports/goldset_availability.json` | Per-paper PMC/OA/licence/retraction status |
| `make eval` | `reports/eval_baseline.json` | Per-group metrics with bootstrap CIs, git rev, platform |
| `make eval-ablations` | `reports/ablation_*.json` | Section-filter and synonym-scope ablations |
| `make qa` | `reports/qa_audit.json` | Provenance rates, constraint violations, retraction hits |
| `make diagnostic` | `reports/diagnostic_benchmark_single.json` | Top-k recall and MRR, ceiling vs pipeline, per case |

Every report embeds the git revision, Python version, and platform, so a mismatch is
attributable.

## Determinism

Deterministic, given the same inputs:

- Bootstrap CIs use a fixed seed (`BOOTSTRAP_SEED = 20260817`).
- The dev/test split hashes the PMID, so it does not depend on iteration order or a shuffle
  seed, and is stable as the gold set grows.
- JATS text normalisation is fixed, so character offsets are stable across runs and releases.
- Ontology pickle caches are keyed on source file mtime + size, so a changed ontology
  invalidates them rather than being silently reused.

**Not pinned:** the ontology releases themselves. `make data` fetches current HPO, MONDO,
HGNC, and HPO annotations, which change monthly. Reproducing an *old* number requires the
ontology versions of that date; the gold set is pinned (phenopacket-store 0.1.27). This is
the main reproducibility gap and it is not yet closed.

## Expected values

Compare against [BENCHMARKS](BENCHMARKS.md). Ontology drift will move phenotype F1 by small
amounts (new HPO terms and synonyms change what grounds); the gold set and the licence audit
should match exactly.

## Common failures

**`FileNotFoundError: .../hp.obo missing. Run: make data-ontologies`** — data step not run.

**`FileNotFoundError: reports/goldset_availability.json missing. Run: make audit`** — the
eval set is defined by the licence audit, so the audit must precede scoring.

**All papers skipped, `papers_skipped_no_cache` equals the paper count** — `make
fetch-fulltext` has not run. Scoring is cache-only by design and will not silently fall back
to the network.

**`SSL: CERTIFICATE_VERIFY_FAILED`** — a python.org install whose
`Install Certificates.command` was never run. All HTTP goes through `requests` (which ships
certifi) specifically to avoid this; if it still appears, a proxy CA is missing from certifi.

**NCBI 429s** — the client limits to 2.8 req/s and retries with backoff. Persistent 429s
mean another process is sharing the quota.
