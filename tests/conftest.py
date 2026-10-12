"""Shared fixtures: raw tables, staged tables, quarantine output and the violations manifest.

Tables are read as text with only empty cells treated as null, so type checks
see exactly what the producer wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from checks import TABLES

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
STAGED_DIR = ROOT / "data" / "staged"
QUARANTINE_DIR = STAGED_DIR / "quarantine"


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.fail(f"{path.relative_to(ROOT)} not found; run `make generate stage` first",
                    pytrace=False)
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


@pytest.fixture(scope="session")
def raw() -> dict[str, pd.DataFrame]:
    return {t: _read(RAW_DIR / f"{t}.csv") for t in TABLES}


@pytest.fixture(scope="session")
def staged() -> dict[str, pd.DataFrame]:
    return {t: _read(STAGED_DIR / f"{t}.csv") for t in TABLES}


@pytest.fixture(scope="session")
def quarantine() -> dict[str, pd.DataFrame]:
    return {t: _read(QUARANTINE_DIR / f"{t}.csv") for t in ("accounts", "daily_balances")}


@pytest.fixture(scope="session")
def manifest() -> dict:
    path = RAW_DIR / "violations_manifest.json"
    if not path.exists():
        pytest.fail(f"{path.relative_to(ROOT)} not found; run `make generate` first", pytrace=False)
    return json.loads(path.read_text())
