"""Shared test fixtures.

The dataset is generated once per session if absent, so a clean checkout can
run `pytest` with no setup step.
"""
from __future__ import annotations

import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

DATASET_DIR = os.path.join(BACKEND_DIR, "eval", "dataset")
FILES_DIR = os.path.join(DATASET_DIR, "files")
CORRUPT_DIR = os.path.join(DATASET_DIR, "corrupt")


@pytest.fixture(scope="session", autouse=True)
def dataset() -> dict:
    """Ensure the labelled dataset exists before any test runs."""
    marker = os.path.join(DATASET_DIR, "ground_truth.json")
    if not os.path.exists(marker):
        from eval.make_dataset import build

        build()
    import json

    with open(marker) as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def dataset_files() -> list[str]:
    return sorted(os.listdir(FILES_DIR))


@pytest.fixture
def ground_truth(dataset: dict) -> dict:
    return dataset
