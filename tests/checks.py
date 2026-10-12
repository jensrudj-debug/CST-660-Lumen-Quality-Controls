"""Detection logic shared by every test layer.

Each check returns the keys of the offending rows rather than a pass/fail
flag, so the same function can assert that staged data is clean and that raw
data contains exactly the defects listed in violations_manifest.json.

This module deliberately does not import scripts/stage.py. The tests are an
independent second implementation of the rules, so a bug in staging cannot
also hide itself from the tests.
"""

from __future__ import annotations

import pandas as pd

NULL_KEY = "<null>"
SPIKE_THRESHOLD = 3.0  # a day-over-day move greater than 300%
MAX_KEYS_SHOWN = 5

TABLES = ["branches", "customers", "accounts", "daily_balances"]

# Column order and logical type for every table. Types are checked against the
# raw text, so "1,234.56" or "03/15/2019" cannot slip through via parsing.
SCHEMAS = {
    "branches": {
        "branch_id": "string", "branch_name": "string", "city": "string",
        "state": "string", "region": "string", "opened_date": "date",
    },
    "customers": {
        "customer_id": "string", "first_name": "string", "last_name": "string",
        "date_of_birth": "date", "email": "string", "home_branch_id": "string",
        "customer_since": "date",
    },
    "accounts": {
        "account_id": "string", "customer_id": "string", "branch_id": "string",
        "account_type": "string", "status": "string", "open_date": "date",
        "currency": "string", "interest_rate": "decimal", "updated_at": "datetime",
    },
    "daily_balances": {
        "account_id": "string", "balance_date": "date",
        "ledger_balance": "decimal", "available_balance": "decimal",
    },
}

TYPE_PATTERNS = {
    "string": r".+",
    "date": r"\d{4}-\d{2}-\d{2}",
    "datetime": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
    "decimal": r"-?\d+(\.\d+)?",
}
DATE_FORMATS = {"date": "%Y-%m-%d", "datetime": "%Y-%m-%dT%H:%M:%S"}

PRIMARY_KEYS = {
    "branches": ["branch_id"],
    "customers": ["customer_id"],
    "accounts": ["account_id"],
    "daily_balances": ["account_id", "balance_date"],
}

# Optional columns: customers.email (not every customer gives one) and
# accounts.interest_rate (non-interest-bearing products). Everything else is required.
OPTIONAL = {"customers": {"email"}, "accounts": {"interest_rate"}}
REQUIRED = {t: [c for c in cols if c not in OPTIONAL.get(t, set())] for t, cols in SCHEMAS.items()}

# (child table, child column, parent table, parent column)
FOREIGN_KEYS = [
    ("accounts", "customer_id", "customers", "customer_id"),
    ("accounts", "branch_id", "branches", "branch_id"),
    ("customers", "home_branch_id", "branches", "branch_id"),
    ("daily_balances", "account_id", "accounts", "account_id"),
]


def row_keys(df: pd.DataFrame, table: str) -> pd.Series:
    """Primary key of each row as text; composite keys are joined with '|'."""
    cols = PRIMARY_KEYS[table]
    keys = df[cols[0]].fillna(NULL_KEY).astype(str)
    for col in cols[1:]:
        keys = keys + "|" + df[col].fillna(NULL_KEY).astype(str)
    return keys


def _keys(df: pd.DataFrame, table: str, mask: pd.Series) -> list[str]:
    return sorted(set(row_keys(df[mask], table)))


def type_violations(df: pd.DataFrame, table: str) -> dict[str, list[str]]:
    """Non-null values that do not match their column's declared type."""
    found = {}
    for col, kind in SCHEMAS[table].items():
        values = df[col]
        present = values.notna()
        bad = present & ~values.fillna("").str.fullmatch(TYPE_PATTERNS[kind])
        if kind in DATE_FORMATS:  # well formed but not a real date, e.g. 2026-02-30
            parsed = pd.to_datetime(values.where(present & ~bad), format=DATE_FORMATS[kind],
                                    errors="coerce")
            bad |= present & parsed.isna()
        if bad.any():
            found[col] = _keys(df, table, bad)
    return found


def duplicate_keys(df: pd.DataFrame, table: str) -> list[str]:
    """Primary-key values that appear on more than one row. Null keys are the not-null layer's job."""
    cols = PRIMARY_KEYS[table]
    complete = df[cols].notna().all(axis=1)
    return _keys(df, table, complete & df.duplicated(cols, keep=False))


def null_violations(df: pd.DataFrame, table: str) -> dict[str, list[str]]:
    """Required columns that contain nulls, with the keys of the affected rows."""
    return {col: _keys(df, table, df[col].isna()) for col in REQUIRED[table] if df[col].isna().any()}


def orphan_keys(tables: dict[str, pd.DataFrame], child: str, col: str,
                parent: str, parent_col: str) -> list[str]:
    """Child rows whose foreign key has no match in the parent. Null FKs are the not-null layer's job."""
    fk = tables[child][col]
    return _keys(tables[child], child, fk.notna() & ~fk.isin(tables[parent][parent_col]))


def join_fanout(tables: dict[str, pd.DataFrame]) -> tuple[int, list[str]]:
    """Extra rows produced by daily_balances LEFT JOIN accounts LEFT JOIN branches.

    Left joins keep orphans at one row each, so any change in row count comes
    only from a duplicated join key. That isolates fan-out from missing parents,
    which the foreign-key checks report separately.

    Returns (extra rows, account_ids whose balance rows multiplied).
    """
    balances = tables["daily_balances"][["account_id"]].reset_index(names="_row")
    joined = (balances
              .merge(tables["accounts"][["account_id", "branch_id"]], on="account_id", how="left")
              .merge(tables["branches"][["branch_id"]], on="branch_id", how="left"))
    per_row = joined.groupby("_row").size()
    fanned_rows = per_row.index[per_row > 1]
    accounts = sorted(set(balances.loc[fanned_rows, "account_id"].dropna()))
    return len(joined) - len(balances), accounts


def balance_spikes(balances: pd.DataFrame) -> list[str]:
    """Rows where ledger_balance moved more than 300% from the account's previous day.

    Move = abs(today - yesterday) / abs(yesterday), flagged when > 3.0.

    The rule needs a well-formed series, so rows with a null or duplicated key
    or a non-numeric balance are left out; the other layers report those.
    "Yesterday" is the account's previous valid row, so a gap left by a dropped
    row does not hide a move across it.
    """
    df = balances[PRIMARY_KEYS["daily_balances"]].copy()
    df["ledger"] = pd.to_numeric(balances["ledger_balance"], errors="coerce")
    valid = (df.notna().all(axis=1)
             & ~df.duplicated(PRIMARY_KEYS["daily_balances"], keep=False))
    df = df[valid].sort_values(PRIMARY_KEYS["daily_balances"])

    prev = df.groupby("account_id")["ledger"].shift()
    # Prior balance of zero: the percentage change is undefined (division by
    # zero), so the move is NOT flagged. 0 -> 0 is no move at all, and
    # 0 -> any amount is how every newly funded account starts, so treating it
    # as an infinite move would flag every account opening. A spike on the
    # following day is still measured against the new non-zero balance.
    pct = (df["ledger"] - prev).abs() / prev.abs().where(prev != 0)
    return _keys(df, "daily_balances", pct > SPIKE_THRESHOLD)


def show(keys) -> str:
    keys = sorted(keys)
    shown = ", ".join(keys[:MAX_KEYS_SHOWN])
    more = f" (+{len(keys) - MAX_KEYS_SHOWN} more)" if len(keys) > MAX_KEYS_SHOWN else ""
    return f"[{shown}]{more}"


def violation(table: str, rule: str, keys) -> str:
    """Failure message for staged data: table, rule and up to five offending keys."""
    return f"{table}: {rule}: {len(keys)} offending key(s): {show(keys)}"


def manifest_mismatch(table: str, rule: str, found, expected) -> str:
    """Failure message when raw detection disagrees with violations_manifest.json."""
    found, expected = set(found), set(expected)
    return (f"{table}: {rule}: raw detection does not match violations_manifest.json; "
            f"found {len(found)}, expected {len(expected)}; "
            f"unexpected keys: {show(found - expected)}; missing keys: {show(expected - found)}")
