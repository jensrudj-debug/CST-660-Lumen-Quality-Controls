"""Unit tests for scripts/validate_contract.py.

Each test writes a small accounts CSV (and, where needed, a modified contract)
to a temp directory, runs the validator as a subprocess exactly as CI does,
and asserts the exit code. Every run passes --base-contract so the result
never depends on what is on origin/main.
"""

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_contract.py"
CONTRACT = ROOT / "contracts" / "accounts.contract.yml"
RUN_DATE = "2026-10-11"  # run time is the end of this day

pytestmark = pytest.mark.contract

CLEAN_ROWS = [
    {"account_id": "A000001", "customer_id": "C00001", "branch_id": "BR001",
     "account_type": "CHECKING", "status": "OPEN", "open_date": "2020-01-15",
     "currency": "USD", "interest_rate": "0.05", "updated_at": "2026-10-11T04:00:00"},
    {"account_id": "A000002", "customer_id": "C00002", "branch_id": "BR042",
     "account_type": "SAVINGS", "status": "DORMANT", "open_date": "2018-06-30",
     "currency": "USD", "interest_rate": "1.25", "updated_at": "2026-10-10T22:30:00"},
    {"account_id": "A000003", "customer_id": "C00003", "branch_id": "BR017",
     "account_type": "MONEY_MARKET", "status": "CLOSED", "open_date": "2026-10-11",
     "currency": "USD", "interest_rate": "", "updated_at": "2026-10-11T01:15:00"},
]


def run(tmp_path, rows=None, contract=None):
    data = tmp_path / "accounts.csv"
    pd.DataFrame(rows or CLEAN_ROWS).to_csv(data, index=False)
    contract_path = CONTRACT
    if contract is not None:
        contract_path = tmp_path / "accounts.contract.yml"
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--contract", str(contract_path), "--data", str(data),
         "--run-date", RUN_DATE, "--base-contract", str(CONTRACT)],
        capture_output=True, text=True)
    return result.returncode, result.stdout


def with_change(**row_changes):
    rows = [dict(r) for r in CLEAN_ROWS]
    rows[0].update(row_changes)
    return rows


def violations(output):
    return [line for line in output.splitlines() if line.startswith("[CONTRACT VIOLATION]")]


def test_clean_data_passes(tmp_path):
    code, out = run(tmp_path)
    assert code == 0, out
    assert violations(out) == []
    assert "PASSED: 0 contract violations" in out


def test_type_mismatch_fails(tmp_path):
    code, out = run(tmp_path, with_change(open_date="03/15/2019"))
    assert code == 1, out
    assert [v for v in violations(out) if v.startswith("[CONTRACT VIOLATION] type(open_date)")], out
    assert "'03/15/2019'" in out and "A000001" in out


def test_null_in_non_nullable_column_fails(tmp_path):
    code, out = run(tmp_path, with_change(customer_id=""))
    assert code == 1, out
    assert violations(out) == [
        "[CONTRACT VIOLATION] nullable(customer_id): 1 null value(s) in a non-nullable "
        "column; keys [A000001]"], out


def test_null_in_nullable_column_passes(tmp_path):
    code, out = run(tmp_path, with_change(interest_rate=""))
    assert code == 0, out


@pytest.mark.parametrize("change, rule", [
    ({"branch_id": "BR043"}, "range(branch_id)"),
    ({"interest_rate": "12.5"}, "range(interest_rate)"),
    ({"account_type": "CRYPTO"}, "allowed_values(account_type)"),
    ({"open_date": "2026-10-12"}, "not_in_future(open_date)"),
], ids=["branch-43", "rate-12.5", "bad-enum", "future-open-date"])
def test_out_of_range_value_fails(tmp_path, change, rule):
    code, out = run(tmp_path, with_change(**change))
    assert code == 1, out
    assert [v for v in violations(out) if v.startswith(f"[CONTRACT VIOLATION] {rule}")], out


def test_stale_freshness_fails(tmp_path):
    # One hour past the 24h SLA, measured from the end of the run date.
    rows = [dict(r, updated_at="2026-10-10T23:00:00") for r in CLEAN_ROWS]
    code, out = run(tmp_path, rows)
    assert code == 1, out
    assert violations(out) == [
        "[CONTRACT VIOLATION] freshness(updated_at): latest updated_at 2026-10-10T23:00:00 "
        "is 25.0h before run time 2026-10-12T00:00:00; SLA is 24h"], out


def test_duplicate_primary_key_fails(tmp_path):
    rows = CLEAN_ROWS + [dict(CLEAN_ROWS[0], status="CLOSED")]
    code, out = run(tmp_path, rows)
    assert code == 1, out
    assert [v for v in violations(out) if "primary_key(account_id)" in v], out


def breaking_contract(version):
    contract = yaml.safe_load(CONTRACT.read_text())
    contract["contract"]["version"] = version
    status = next(c for c in contract["schema"]["columns"] if c["name"] == "status")
    status["allowed_values"].remove("DORMANT")
    return contract


def test_breaking_change_without_major_bump_fails(tmp_path):
    # Removing an enum value is BREAKING. The data has no DORMANT rows, so the
    # only possible failure is the version policy itself.
    rows = [dict(r, status="OPEN") for r in CLEAN_ROWS]
    code, out = run(tmp_path, rows, breaking_contract("1.1.0"))
    assert code == 1, out
    assert violations(out) == [
        "[CONTRACT VIOLATION] breaking_change: 'status' allowed value 'DORMANT' removed is "
        "BREAKING (remove an allowed enum value) but version 1.0.0 -> 1.1.0 is not a major "
        "bump; requires a major version bump and consumer sign-off"], out


def test_breaking_change_with_major_bump_passes(tmp_path):
    rows = [dict(r, status="OPEN") for r in CLEAN_ROWS]
    code, out = run(tmp_path, rows, breaking_contract("2.0.0"))
    assert code == 0, out
