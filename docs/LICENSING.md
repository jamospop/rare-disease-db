# Licensing

Conservative by construction. Where the rules are ambiguous we take the restrictive
reading and accept the lost coverage, because the alternative - being wrong at corpus
scale, across thousands of publishers - is not recoverable.

## The distinction everything rests on

**Facts are not expression.** That a paper reports microcephaly in a 3-year-old is a fact
about the paper. The author's sentence describing it is their expression. We extract and
republish facts freely; we republish sentences only where the licence permits it.

A character offset is likewise a fact *about* a public document, not a copy of it. So
every record carries `source_id` + `start`/`end` regardless of licence, and a reader who
has lawful access to the source can always verify an assertion even when we cannot show
them the sentence.

## What we read

Full text is read **only** from the PMC Open Access subset, as reported by the PMC OA
service (`oa.fcgi`) at fetch time - never inferred from the journal, the publisher, or the
presence of a PMCID. Being *in* PMC is not the same as being in the OA subset: of 1,733
gold-set papers, 1,023 are in PMC and only 647 are in the OA subset.

For everything else we use title, abstract, and metadata via PubMed, plus an outbound
link. No scraping of publisher sites, no interlibrary copies, no PDF harvesting.

## What we publish, by tier

The tier is computed per source and stored on every record (`SourceDoc`), so a downstream
user can filter without re-deriving it.

| Tier | Licence | Facts + offsets | Verbatim quotes |
|---|---|---|---|
| `full_text_quotable` | CC0, CC BY, CC BY-SA | yes | **yes** |
| `full_text_facts_only` | CC BY-NC, CC BY-ND, CC BY-NC-SA, CC BY-NC-ND, or unstated | yes | no |
| `abstract_only` | not in the OA subset | yes (abstract-derived only) | no |

`Evidence.quote` is populated only when `SourceDoc.quotes_permitted` is true. This is
enforced in one place (`rdcd/extract/base.py::make_evidence`) rather than trusted to each
extractor, and it is unit-tested
(`tests/test_llm_extractor.py::test_quote_withheld_when_licence_forbids_it`).

### Why ND and NC both land in facts-only

- **NoDerivatives**: a structured extraction is plausibly a derivative work. We do not
  litigate it; we withhold the expression and keep the facts.
- **NonCommercial**: the dataset is CC-BY, which permits commercial reuse. Redistributing
  NC-licensed prose inside a CC-BY dataset would misrepresent the source's terms to
  downstream users. Facts carry no such restriction.

This costs 205 papers and 1,147 gold cases' worth of quotable evidence. That is the price
of a licence tier a downstream user can rely on without checking our work.

## What this repository is licensed as

- **Code**: Apache-2.0.
- **Extracted data**: CC-BY-4.0, with per-record source attribution, licence, and tier.
- **Ontology inputs**: HPO and MONDO are CC BY 4.0; HGNC is CC0; Retraction Watch via
  Crossref is CC BY 4.0. None are vendored; `make data` fetches them so the user gets them
  under their own terms.

Neither licence extends to the underlying publications. Cite the source paper, not this
database, when the claim is the paper's.

## Retractions

Retracted sources are flagged, never removed (see DECISIONS D18/D19). A record from a
retracted paper keeps its data, gains a flag and a notice, and is excluded from eval sets
by default (`build_eval_papers(include_retracted=False)`).

## Rate limits and terms of service

NCBI's polite-use limits are honoured centrally: 2.8 req/s without an API key, 9 req/s
with `NCBI_API_KEY`, with `tool` and `email` on every request as E-utilities requires.
Everything is cached, so re-running analyses does not re-hit the service.

## What a lawyer might have won

An open question we have deliberately resolved against ourselves: whether structured
extraction from ND-licensed text is fair use / fair dealing, and whether text-and-data-mining
exceptions permit full-text mining beyond the OA subset for non-commercial research.

Two specific provisions are worth proper legal review rather than our guesswork:

- **UK CDPA s.29A** - a copyright exception for computational analysis for non-commercial
  research, where the researcher already has lawful access to the work.
- **EU DSM Directive Art. 3** - a TDM exception for research organisations, again predicated
  on lawful access.

Both are *cited here as questions, not conclusions.* Neither has been verified against the
current statute, the case law, or this project's specific facts, and nothing in this
repository relies on either. The relevant unknowns are at least: what "lawful access" means
for a personal or institutional subscription, whether a structured extraction counts as a
permitted copy or a new derivative work, whether the extracted database may then be
*redistributed* (a separate question from whether it may be *made*), and whether
"non-commercial" survives publishing under CC BY.

**What that would be worth if it resolved favourably:** §7 of BENCHMARKS puts a number on it.
The 1,086 `abstract_only` papers currently contribute full-quality gene and diagnosis data
but only ~half the phenotype signal and essentially no negative findings. Full-text access to
that tier is the difference between ~4,100 and ~10,400 usable gold cases, and would roughly
double corpus coverage.

We built for the restrictive reading because the plan has no lawyer in it, and a licensing
error discovered after publication cannot be un-published. Resolving this is a legal work
item, not an engineering one.
