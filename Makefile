# Open Rare-Disease Case Database
#
# The contract this file encodes: `make reproduce` regenerates every number in
# the README and docs/BENCHMARKS.md from public sources, with no API key and no
# account. Fetching is separated from scoring so that scoring is offline and
# cannot drift with whatever NCBI served that afternoon.

PY := python3
DATA := data
ONT := $(DATA)/ontologies
GOLD := $(DATA)/goldsets
REPORTS := reports

.DEFAULT_GOAL := help
.PHONY: help install data data-ontologies data-goldsets audit fetch-fulltext \
        eval eval-ablations diagnostic qa reproduce test lint manifest check \
        clean clean-cache release api

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	$(PY) -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Data acquisition (network). Each target is idempotent.
# ---------------------------------------------------------------------------
data: data-ontologies data-goldsets  ## Download ontologies and gold sets

data-ontologies: $(ONT)/hp.obo $(ONT)/mondo.obo $(ONT)/hgnc_complete_set.txt \
                 $(ONT)/phenotype.hpoa $(ONT)/genes_to_disease.txt  ## Download HPO/MONDO/HGNC/HPOA

$(ONT)/hp.obo:
	@mkdir -p $(ONT)
	curl -sL -o $@ https://purl.obolibrary.org/obo/hp.obo

$(ONT)/mondo.obo:
	@mkdir -p $(ONT)
	curl -sL -o $@ https://purl.obolibrary.org/obo/mondo.obo

$(ONT)/hgnc_complete_set.txt:
	@mkdir -p $(ONT)
	curl -sL -o $@ https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt

$(ONT)/phenotype.hpoa:
	@mkdir -p $(ONT)
	curl -sL -o $@ http://purl.obolibrary.org/obo/hp/hpoa/phenotype.hpoa

$(ONT)/genes_to_disease.txt:
	@mkdir -p $(ONT)
	curl -sL -o $@ https://purl.obolibrary.org/obo/hp/hpoa/genes_to_disease.txt

data-goldsets: $(GOLD)/all_phenopackets.zip  ## Download GA4GH phenopacket-store
	@mkdir -p $(GOLD)/phenopacket-store
	cd $(GOLD) && unzip -q -o all_phenopackets.zip -d phenopacket-store
	@echo "gold phenopackets: $$(find $(GOLD)/phenopacket-store -name '*.json' | wc -l)"

$(GOLD)/all_phenopackets.zip:
	@mkdir -p $(GOLD)
	curl -sL -o $@ https://github.com/monarch-initiative/phenopacket-store/releases/download/0.1.27/all_phenopackets.zip

# ---------------------------------------------------------------------------
# Eval pipeline
# ---------------------------------------------------------------------------
audit: $(REPORTS)/goldset_availability.json  ## Which gold sources may we legally read?

$(REPORTS)/goldset_availability.json: data
	$(PY) -u scripts/audit_goldset.py

fetch-fulltext: audit  ## Cache JATS full text for the eval set (network, rate-limited)
	$(PY) -u scripts/fetch_eval_fulltext.py

eval: audit  ## Score the dictionary baseline (offline; needs fetch-fulltext first)
	$(PY) -u scripts/run_eval.py --name baseline

eval-ablations: audit  ## Ablations justifying the baseline's design choices
	$(PY) -u scripts/run_eval.py --all-sections        --name ablation_all_sections
	$(PY) -u scripts/run_eval.py --no-related-synonyms --name ablation_no_related_syn
	$(PY) -u scripts/run_eval.py --no-phenotype-root    --name ablation_no_pheno_root

diagnostic: audit  ## Ceiling vs pipeline top-k diagnostic recall
	$(PY) -u scripts/run_diagnostic_benchmark.py single

qa: audit  ## Constraint + provenance audit over gold and predictions
	$(PY) -u scripts/run_qa_audit.py

# Includes fetch-fulltext so a fresh clone actually works: scoring is cache-only
# by design, so without it every paper is skipped rather than silently refetched.
reproduce: data fetch-fulltext eval eval-ablations qa diagnostic  ## Regenerate every published number
	@echo
	@echo "All reports written to $(REPORTS)/. Compare against docs/BENCHMARKS.md."

api:  ## Serve the read API and reference UI on :8080
	$(PY) -u scripts/serve_api.py

release:  ## Build a versioned dataset release (JSONL + phenopackets + checksums)
	$(PY) -u scripts/build_release.py

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------
test:  ## Run the test suite (no network; 41 tests skip until `make data`)
	$(PY) -m pytest tests/ -q

lint:  ## Byte-compile everything as a cheap syntax check
	$(PY) -m compileall -q rdcd scripts tests && echo "ok"

manifest:  ## Regenerate FILES.md; fails if any file is undocumented
	$(PY) scripts/gen_manifest.py

check: lint test manifest  ## Everything that must pass before shipping
	@echo "all checks passed"

clean:  ## Remove generated reports (keeps downloaded data)
	rm -rf $(REPORTS)/*.json $(REPORTS)/*.log

clean-cache:  ## Remove the HTTP cache (forces refetch; loses reproducibility)
	rm -rf $(DATA)/cache
