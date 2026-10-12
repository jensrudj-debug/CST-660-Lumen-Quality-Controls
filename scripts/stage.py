"""Stage raw core-banking tables: quarantine bad rows, pass clean rows through.

Reads data/raw/ and writes:
    data/staged/<table>.csv             clean rows
    data/staged/quarantine/<table>.csv  rejected rows plus a quarantine_reason column

Only accounts and daily_balances are filtered. branches.csv is a trusted
reference table and is copied byte for byte, defects included: a duplicate
branch_id must fail the test suite loudly, not be deduped here where it would
hide the problem upstream. customers.csv is copied through unchanged.

A row can fail several checks; quarantine_reason lists every one, separated
by "; ".
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
STAGED_DIR = ROOT / "data" / "staged"

SPIKE_THRESHOLD = 3.0  # flag a day-over-day move greater than 300%

ACCOUNTS_REQUIRED = ["account_id", "customer_id", "branch_id", "account_type", "status",
                     "open_date", "currency", "updated_at"]
BALANCES_REQUIRED = ["account_id", "balance_date", "ledger_balance", "available_balance"]
BALANCES_PK = ["account_id", "balance_date"]

# Typed columns and the exact text format each must match.
DATE = r"\d{4}-\d{2}-\d{2}"
ACCOUNTS_TYPES = {"open_date": ("date", DATE), "interest_rate": ("decimal", r"-?\d+(\.\d+)?"),
                  "updated_at": ("datetime", DATE + r"T\d{2}:\d{2}:\d{2}")}
BALANCES_TYPES = {"balance_date": ("date", DATE),
                  "ledger_balance": ("decimal", r"-?\d+(\.\d+)?"),
                  "available_balance": ("decimal", r"-?\d+(\.\d+)?")}
DATE_FORMATS = {"date": "%Y-%m-%d", "datetime": "%Y-%m-%dT%H:%M:%S"}


def read(path: Path) -> pd.DataFrame:
    # Read everything as text so staged files keep the raw values exactly.
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


def add_reason(reasons: pd.Series, mask: pd.Series, reason: str) -> None:
    for i in mask[mask].index:
        reasons[i].append(reason)


def null_reasons(df: pd.DataFrame, required: list[str], reasons: pd.Series) -> None:
    for col in required:
        add_reason(reasons, df[col].isna(), f"null_required:{col}")


def type_reasons(df: pd.DataFrame, types: dict, reasons: pd.Series) -> None:
    for col, (kind, pattern) in types.items():
        present = df[col].notna()
        bad = present & ~df[col].fillna("").str.fullmatch(pattern)
        if kind in DATE_FORMATS:  # well formed but not a real date, e.g. 2026-02-30
            parsed = pd.to_datetime(df[col].where(present & ~bad), format=DATE_FORMATS[kind],
                                    errors="coerce")
            bad |= present & parsed.isna()
        add_reason(reasons, bad, f"invalid_type:{col}")


def check_accounts(accounts: pd.DataFrame, customers: pd.DataFrame,
                   branches: pd.DataFrame) -> pd.Series:
    reasons = pd.Series([[] for _ in range(len(accounts))], index=accounts.index)
    null_reasons(accounts, ACCOUNTS_REQUIRED, reasons)
    type_reasons(accounts, ACCOUNTS_TYPES, reasons)

    # Every copy of a duplicated key is quarantined: there is no way to know
    # which version is correct.
    ids = accounts["account_id"]
    add_reason(reasons, ids.notna() & ids.duplicated(keep=False), "duplicate_account_id")

    cust = accounts["customer_id"]
    add_reason(reasons, cust.notna() & ~cust.isin(customers["customer_id"]), "orphan_customer_id")
    branch = accounts["branch_id"]
    add_reason(reasons, branch.notna() & ~branch.isin(branches["branch_id"]), "orphan_branch_id")
    return reasons


def check_balances(balances: pd.DataFrame, raw_accounts: pd.DataFrame,
                   clean_account_ids: pd.Series) -> pd.Series:
    reasons = pd.Series([[] for _ in range(len(balances))], index=balances.index)
    null_reasons(balances, BALANCES_REQUIRED, reasons)
    type_reasons(balances, BALANCES_TYPES, reasons)

    pk_complete = balances[BALANCES_PK].notna().all(axis=1)
    add_reason(reasons, pk_complete & balances.duplicated(BALANCES_PK, keep=False), "duplicate_pk")

    # Separate "never existed" from "existed but was quarantined", because
    # they point at different root causes: the first is a balances feed
    # problem, the second is downstream fallout from a bad account row.
    acct = balances["account_id"]
    unknown = acct.notna() & ~acct.isin(raw_accounts["account_id"])
    add_reason(reasons, unknown, "unknown_account_id")
    add_reason(reasons, acct.notna() & ~unknown & ~acct.isin(clean_account_ids),
               "parent_account_quarantined")

    # Business rule: run on rows that passed every other check, so a null,
    # duplicate or malformed number cannot create or hide a spike.
    ok = reasons.map(len) == 0
    series = balances.loc[ok, BALANCES_PK].copy()
    series["ledger"] = pd.to_numeric(balances.loc[ok, "ledger_balance"])
    series = series.sort_values(BALANCES_PK)
    prev = series.groupby("account_id")["ledger"].shift()
    # A move from zero has no defined percentage, so it is not flagged.
    pct = (series["ledger"] - prev).abs() / prev.abs().where(prev != 0)
    spike_rows = pct.index[pct > SPIKE_THRESHOLD]
    spike_accounts = series.loc[spike_rows, "account_id"].unique()

    add_reason(reasons, pd.Series(balances.index.isin(spike_rows), index=balances.index),
               "balance_move_over_300pct")
    sibling = ok & acct.isin(spike_accounts) & ~balances.index.isin(spike_rows)
    add_reason(reasons, sibling, "account_has_balance_move_over_300pct")
    return reasons


def split(df: pd.DataFrame, reasons: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    bad = reasons.map(len) > 0
    quarantined = df[bad].copy()
    quarantined["quarantine_reason"] = reasons[bad].map("; ".join)
    return df[~bad], quarantined


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=STAGED_DIR)
    args = parser.parse_args()

    quarantine_dir = args.out / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    branches = read(args.raw / "branches.csv")
    customers = read(args.raw / "customers.csv")
    accounts = read(args.raw / "accounts.csv")
    balances = read(args.raw / "daily_balances.csv")

    clean_accounts, bad_accounts = split(accounts, check_accounts(accounts, customers, branches))
    clean_balances, bad_balances = split(
        balances, check_balances(balances, accounts, clean_accounts["account_id"]))

    shutil.copyfile(args.raw / "branches.csv", args.out / "branches.csv")
    shutil.copyfile(args.raw / "customers.csv", args.out / "customers.csv")
    for name, clean, bad in (("accounts", clean_accounts, bad_accounts),
                             ("daily_balances", clean_balances, bad_balances)):
        clean.to_csv(args.out / f"{name}.csv", index=False)
        bad.to_csv(quarantine_dir / f"{name}.csv", index=False)

    print("Staging summary")
    print(f"  {'table':<16} {'raw':>8} {'kept':>8} {'quarantined':>12}")
    print(f"  {'branches':<16} {len(branches):>8,} {len(branches):>8,} {'passthrough':>12}")
    print(f"  {'customers':<16} {len(customers):>8,} {len(customers):>8,} {'passthrough':>12}")
    for name, raw, clean, bad in (("accounts", accounts, clean_accounts, bad_accounts),
                                  ("daily_balances", balances, clean_balances, bad_balances)):
        print(f"  {name:<16} {len(raw):>8,} {len(clean):>8,} {len(bad):>12,}")

    for name, bad in (("accounts", bad_accounts), ("daily_balances", bad_balances)):
        print(f"Quarantine reasons: {name} (rows can carry more than one)")
        counts = bad["quarantine_reason"].str.split("; ").explode().value_counts()
        for reason, n in counts.sort_index().items():
            print(f"  {reason:<40} {n:>6,}")


if __name__ == "__main__":
    main()
