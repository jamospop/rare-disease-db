"""Test configuration.

Some tests need the downloaded ontologies (HPO, MONDO, HGNC, HPO annotations) and the gold
set; the rest are pure unit tests. A fresh clone that has not run `make data` should get a
clear skip with the command to fix it, not a wall of red FileNotFoundErrors that looks like
the code is broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ONT = ROOT / "data" / "ontologies"

REQUIRED_ONTOLOGIES = ("hp.obo", "mondo.obo", "hgnc_complete_set.txt", "phenotype.hpoa")

# Test modules that touch the ontology store. Kept explicit rather than inferred so
# adding a module is a deliberate choice.
NEEDS_ONTOLOGIES = ("test_ontology", "test_metrics", "test_qa", "test_llm_extractor")


def _missing() -> list[str]:
    return [f for f in REQUIRED_ONTOLOGIES if not (ONT / f).exists()]


def pytest_collection_modifyitems(config, items):
    missing = _missing()
    if not missing:
        return
    skip = pytest.mark.skip(
        reason=(
            f"needs downloaded ontologies ({', '.join(missing)}). Run: make data"
        )
    )
    for item in items:
        if any(name in item.nodeid for name in NEEDS_ONTOLOGIES):
            item.add_marker(skip)


def pytest_report_header(config):
    missing = _missing()
    if missing:
        return (
            f"rdcd: ontologies not downloaded ({len(missing)} missing) - "
            f"ontology-dependent tests will SKIP. Run `make data` to enable them."
        )
    return "rdcd: ontologies present - full test suite enabled"
