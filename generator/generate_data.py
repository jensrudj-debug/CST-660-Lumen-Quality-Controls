"""Generate a synthetic core-banking dataset for Lumen Community Bank.

Writes to data/raw/:
    branches.csv              42-branch reference table
    customers.csv             customer master
    accounts.csv              deposit accounts
    daily_balances.csv        one end-of-day balance per account per day
    violations_manifest.json  every planted defect, keyed the way the tests report it

Defects are planted on purpose so the staging step and the test suite have
something to catch. The manifest is built from what was planted, not by
running the checks, so the tests have an independent answer key.

Row keys in the manifest are the table's primary key. daily_balances uses
"account_id|balance_date", and a missing key part is written as "<null>".

Output is deterministic for a given seed, except that balance dates end on the
run date so the freshness SLA in the accounts contract can be checked.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
NULL_KEY = "<null>"

N_BRANCHES = 42
N_CUSTOMERS = 1_000
N_ACCOUNTS = 1_500
N_DAYS = 30

# How many of each defect to plant.
PLANTED = {
    "accounts.orphan_customer_id": 5,
    "accounts.orphan_branch_id": 4,
    "accounts.duplicate_account_id": 3,
    "accounts.null_required": 4,
    "accounts.invalid_open_date": 2,
    "daily_balances.unknown_account_id": 3,
    "daily_balances.duplicate_pk": 5,
    "daily_balances.null_required": 4,
    "daily_balances.invalid_ledger_balance": 2,
    "daily_balances.spike_over_300pct": 6,
}
DUPLICATE_BRANCH_ID = "BR017"

STATES = {
    "IA": ["Des Moines", "Cedar Rapids", "Davenport", "Ames"],
    "IL": ["Peoria", "Rockford", "Springfield", "Champaign"],
    "MN": ["Rochester", "Duluth", "St. Cloud", "Mankato"],
    "WI": ["Madison", "Green Bay", "Eau Claire", "La Crosse"],
    "MO": ["Columbia", "Springfield", "Joplin"],
    "NE": ["Omaha", "Lincoln", "Kearney"],
}
REGIONS = {"IA": "Central", "NE": "Central", "MO": "Central",
           "IL": "East", "WI": "North", "MN": "North"}

FIRST_NAMES = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
               "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
               "Joseph", "Jessica", "Thomas", "Sarah", "Daniel", "Karen", "Maria",
               "Ahmed", "Mei", "Carlos", "Priya", "Olga", "Kwame", "Hana"]
LAST_NAMES = ["Anderson", "Johnson", "Peterson", "Olson", "Nelson", "Larson", "Miller",
              "Schmidt", "Garcia", "Nguyen", "Kowalski", "Hansen", "Becker", "Meyer",
              "Lopez", "Patel", "Kim", "Okafor", "Novak", "Lindqvist"]

ACCOUNT_TYPES = ["CHECKING", "SAVINGS", "MONEY_MARKET", "CD"]
ACCOUNT_TYPE_WEIGHTS = [0.50, 0.30, 0.12, 0.08]
RATE_RANGE = {"CHECKING": (0.0, 0.10), "SAVINGS": (0.25, 1.50),
              "MONEY_MARKET": (1.00, 3.00), "CD": (2.50, 5.00)}
STATUSES = ["OPEN", "DORMANT", "CLOSED"]
STATUS_WEIGHTS = [0.90, 0.07, 0.03]


def make_branches(rng: np.random.Generator) -> pd.DataFrame:
    cities = [(state, city) for state, cs in STATES.items() for city in cs]
    rows = []
    for i in range(N_BRANCHES):
        state, city = cities[i % len(cities)]
        suffix = "" if i < len(cities) else " West"
        rows.append({
            "branch_id": f"BR-{i + 1:03d}",
            "branch_name": f"Lumen {city}{suffix}",
            "city": city,
            "state": state,
            "region": REGIONS[state],
            "opened_date": date(1985, 1, 1) + timedelta(days=int(rng.integers(0, 13_000))),
        })
    branches = pd.DataFrame(rows)

    # Defect: a second row for an existing branch_id, like a re-keyed branch
    # that was appended instead of updated. Any join to this table on
    # branch_id fans out and double counts that branch's accounts, which is
    # the failure from last quarter's deposits packet.
    dup = branches.loc[branches["branch_id"].isin(["BR-005", "BR-017", "BR-031"])].copy()
    dup["branch_name"] = dup["branch_name"].str.replace("Lumen ", "Lumen Community Bank ")
    return pd.concat([branches, dup], ignore_index=True)


def make_customers(rng: np.random.Generator, branch_ids: list[str], today: date) -> pd.DataFrame:
    n = N_CUSTOMERS
    first = rng.choice(FIRST_NAMES, n)
    last = rng.choice(LAST_NAMES, n)
    ids = [f"C{i + 1:05d}" for i in range(n)]
    dob = [date(1940, 1, 1) + timedelta(days=int(d)) for d in rng.integers(0, 23_000, n)]
    since = [today - timedelta(days=int(d)) for d in rng.integers(30, 9_000, n)]
    return pd.DataFrame({
        "customer_id": ids,
        "first_name": first,
        "last_name": last,
        "date_of_birth": dob,
        "email": [f"{f.lower()}.{l.lower()}{i[1:]}@example.com" for f, l, i in zip(first, last, ids)],
        "home_branch_id": rng.choice(branch_ids, n),
        "customer_since": since,
    })


def make_accounts(rng: np.random.Generator, customers: pd.DataFrame, branch_ids: list[str],
                  today: date) -> pd.DataFrame:
    n = N_ACCOUNTS
    cust_idx = rng.integers(0, len(customers), n)
    cust = customers.iloc[cust_idx].reset_index(drop=True)
    types = rng.choice(ACCOUNT_TYPES, n, p=ACCOUNT_TYPE_WEIGHTS)
    rates = [round(float(rng.uniform(*RATE_RANGE[t])), 2) for t in types]

    # Most accounts are held at the customer's home branch.
    at_home = rng.random(n) < 0.85
    branch = np.where(at_home, cust["home_branch_id"], rng.choice(branch_ids, n))

    open_dates = [max(s, today - timedelta(days=int(d)))
                  for s, d in zip(cust["customer_since"], rng.integers(N_DAYS + 1, 8_000, n))]
    run_ts = datetime.combine(today, time(5, 0))
    updated = [run_ts - timedelta(minutes=int(m)) for m in rng.integers(0, 600, n)]

    accounts = pd.DataFrame({
        "account_id": [f"A{i + 1:06d}" for i in range(n)],
        "customer_id": cust["customer_id"],
        "branch_id": branch,
        "account_type": types,
        "status": rng.choice(STATUSES, n, p=STATUS_WEIGHTS),
        "open_date": open_dates,
        "currency": "USD",
        "interest_rate": rates,
        "updated_at": [u.strftime("%Y-%m-%dT%H:%M:%S") for u in updated],
    })
    accounts["open_date"] = accounts["open_date"].astype(str)
    return accounts


def plant_account_defects(rng: np.random.Generator, accounts: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Corrupt a few account rows. Returns the table and the affected account_ids."""
    accounts = accounts.astype({"customer_id": object, "branch_id": object,
                                "account_type": object, "open_date": object})
    pool = list(rng.permutation(len(accounts)))
    take = lambda k: [pool.pop() for _ in range(k)]  # noqa: E731
    planted: dict = {}

    idx = take(PLANTED["accounts.orphan_customer_id"])
    accounts.loc[idx, "customer_id"] = [f"C9{i:04d}" for i in range(len(idx))]
    planted["orphan_customer_id"] = accounts.loc[idx, "account_id"].tolist()

    idx = take(PLANTED["accounts.orphan_branch_id"])
    accounts.loc[idx, "branch_id"] = ["BR043", "BR099", "BR000", "BR143"][: len(idx)]
    planted["orphan_branch_id"] = accounts.loc[idx, "account_id"].tolist()

    idx = take(PLANTED["accounts.null_required"])
    planted["null_required"] = {}
    for i, col in zip(idx, ["customer_id", "account_type", "open_date", "branch_id"]):
        accounts.loc[i, col] = None
        planted["null_required"].setdefault(col, []).append(accounts.loc[i, "account_id"])

    # Wrong type: a US-style date from a spreadsheet export instead of ISO 8601.
    idx = take(PLANTED["accounts.invalid_open_date"])
    for i in idx:
        d = date.fromisoformat(accounts.loc[i, "open_date"])
        accounts.loc[i, "open_date"] = f"{d.month:02d}/{d.day:02d}/{d.year}"
    planted["invalid_open_date"] = accounts.loc[idx, "account_id"].tolist()

    # Duplicate keys: re-emit an existing account_id with a different type,
    # as a buggy upsert would. Neither copy can be trusted over the other.
    idx = take(PLANTED["accounts.duplicate_account_id"])
    dups = accounts.loc[idx].copy()
    dups["account_type"] = ["SAVINGS" if t == "CHECKING" else "CHECKING" for t in dups["account_type"]]
    planted["duplicate_account_id"] = dups["account_id"].tolist()

    return pd.concat([accounts, dups], ignore_index=True), planted


def make_balances(rng: np.random.Generator, accounts: pd.DataFrame, today: date) -> pd.DataFrame:
    """Random walk of end-of-day balances, one row per unique account per day."""
    ids = accounts["account_id"].drop_duplicates().tolist()
    days = [today - timedelta(days=d) for d in range(N_DAYS - 1, -1, -1)]

    start = rng.lognormal(mean=8.5, sigma=1.2, size=len(ids)).clip(50, 750_000)
    # Daily moves of at most +/-8% keep every clean account far below 300%.
    steps = rng.uniform(-0.08, 0.08, size=(len(ids), N_DAYS))
    steps[:, 0] = 0.0
    walk = start[:, None] * np.cumprod(1 + steps, axis=1)

    ledger = np.round(walk, 2)
    holds = np.round(ledger * rng.uniform(0, 0.03, size=ledger.shape), 2)
    return pd.DataFrame({
        "account_id": np.repeat(ids, N_DAYS),
        "balance_date": np.tile([d.isoformat() for d in days], len(ids)),
        "ledger_balance": ledger.ravel(),
        "available_balance": np.round(ledger - holds, 2).ravel(),
    })


def bal_key(account_id: str | None, balance_date: str | None) -> str:
    return f"{account_id or NULL_KEY}|{balance_date or NULL_KEY}"


def plant_balance_defects(rng: np.random.Generator, balances: pd.DataFrame,
                          skip_accounts: set[str]) -> tuple[pd.DataFrame, dict]:
    """Corrupt a few balance rows. Returns the table and the affected row keys."""
    candidates = sorted(set(balances["account_id"]) - skip_accounts)
    # Copy a clean series for the ghost accounts before anything is corrupted.
    ghost_template = balances[balances["account_id"] == candidates[0]].copy()

    balances = balances.astype({"account_id": object, "balance_date": object,
                                "ledger_balance": object, "available_balance": object})
    planted: dict = {}

    # Each defect goes on a different otherwise-clean account, so every
    # planted row has exactly one cause.
    n_targets = sum(PLANTED[k] for k in ("daily_balances.spike_over_300pct",
                                         "daily_balances.null_required",
                                         "daily_balances.duplicate_pk",
                                         "daily_balances.invalid_ledger_balance"))
    chosen = list(rng.choice(candidates[1:], size=n_targets, replace=False))

    def pick_row(acct: str, lo: int = 0, hi: int | None = None) -> int:
        rows = balances.index[balances["account_id"] == acct]
        return rows[int(rng.integers(lo, hi if hi is not None else len(rows)))]

    # Spikes: one day where the balance jumps to 5-8x the previous day, as if
    # an amount was keyed in cents instead of dollars or a decimal was lost.
    planted["spike_over_300pct"] = []
    for _ in range(PLANTED["daily_balances.spike_over_300pct"]):
        acct = chosen.pop()
        i = pick_row(acct, 10, N_DAYS - 1)
        factor = float(rng.uniform(5, 8))
        balances.loc[i, "ledger_balance"] = round(balances.loc[i - 1, "ledger_balance"] * factor, 2)
        balances.loc[i, "available_balance"] = balances.loc[i, "ledger_balance"]
        planted["spike_over_300pct"].append(bal_key(acct, balances.loc[i, "balance_date"]))

    planted["null_required"] = {}
    for col in ["ledger_balance", "balance_date", "available_balance", "account_id"]:
        acct = chosen.pop()
        i = pick_row(acct)
        balances.loc[i, col] = None
        key = bal_key(balances.loc[i, "account_id"], balances.loc[i, "balance_date"])
        planted["null_required"].setdefault(col, []).append(key)

    # Wrong type: a balance formatted with a thousands separator.
    planted["invalid_ledger_balance"] = []
    for _ in range(PLANTED["daily_balances.invalid_ledger_balance"]):
        acct = chosen.pop()
        i = pick_row(acct)
        balances.loc[i, "ledger_balance"] = f"{float(balances.loc[i, 'ledger_balance']) + 1000:,.2f}"
        planted["invalid_ledger_balance"].append(bal_key(acct, balances.loc[i, "balance_date"]))

    # Duplicate (account_id, balance_date): a reloaded day with a slightly
    # different figure.
    dup_rows = []
    planted["duplicate_pk"] = []
    for _ in range(PLANTED["daily_balances.duplicate_pk"]):
        acct = chosen.pop()
        row = balances.loc[pick_row(acct)].copy()
        row["ledger_balance"] = round(row["ledger_balance"] + 100.0, 2)
        dup_rows.append(row)
        planted["duplicate_pk"].append(bal_key(acct, row["balance_date"]))

    # Balances for accounts that do not exist in accounts.csv.
    ghosts = [ghost_template.assign(account_id=f"A9{i:05d}")
              for i in range(PLANTED["daily_balances.unknown_account_id"])]
    planted["unknown_account_id"] = [bal_key(a, d) for g in ghosts
                                     for a, d in zip(g["account_id"], g["balance_date"])]

    out = pd.concat([balances, pd.DataFrame(dup_rows), *ghosts], ignore_index=True)
    return out, planted


def expected_fanout(branches: pd.DataFrame, accounts: pd.DataFrame,
                    balances: pd.DataFrame) -> dict:
    """Rows a daily_balances -> accounts -> branches left join adds, from the planted duplicates.

    Each balance row turns into (account rows for its id) x (branch rows for
    each of those accounts' branch_id) rows; an unmatched key still yields one.
    """
    branch_rows = branches["branch_id"].value_counts()
    per_account = accounts.groupby("account_id")["branch_id"].agg(
        lambda s: sum(int(branch_rows.get(b, 1)) for b in s))
    bal_rows = balances["account_id"].value_counts()
    fanned = sorted(a for a in bal_rows.index if per_account.get(a, 1) > 1)
    extra = sum(int(bal_rows[a]) * (int(per_account[a]) - 1) for a in fanned)
    return {"account_ids": fanned, "extra_rows": extra}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=660)
    parser.add_argument("--out", type=Path, default=RAW_DIR)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    today = date.today()
    args.out.mkdir(parents=True, exist_ok=True)

    branches = make_branches(rng)
    branch_ids = branches["branch_id"].drop_duplicates().tolist()
    customers = make_customers(rng, branch_ids, today)
    accounts = make_accounts(rng, customers, branch_ids, today)
    accounts, acct = plant_account_defects(rng, accounts)

    balances = make_balances(rng, accounts, today)
    bad_accounts = {a for v in acct.values() for a in (v if isinstance(v, list) else sum(v.values(), []))}
    balances, bal = plant_balance_defects(rng, balances, bad_accounts)

    tables = {"branches": branches, "customers": customers,
              "accounts": accounts, "daily_balances": balances}
    for name, df in tables.items():
        df.to_csv(args.out / f"{name}.csv", index=False)

    s = sorted
    manifest = {
        "seed": args.seed,
        "as_of": today.isoformat(),
        "schema": {
            "accounts": {"open_date": s(acct["invalid_open_date"])},
            "daily_balances": {"ledger_balance": s(bal["invalid_ledger_balance"])},
        },
        "pk": {
            "branches": [DUPLICATE_BRANCH_ID],
            "customers": [],
            "accounts": s(acct["duplicate_account_id"]),
            "daily_balances": s(bal["duplicate_pk"]),
        },
        "relationships": {
            "accounts.customer_id": s(acct["orphan_customer_id"]),
            "accounts.branch_id": s(acct["orphan_branch_id"]),
            "customers.home_branch_id": [],
            "daily_balances.account_id": s(bal["unknown_account_id"]),
        },
        "join_fanout": expected_fanout(branches, accounts, balances),
        "not_null": {
            "accounts": {c: s(v) for c, v in acct["null_required"].items()},
            "daily_balances": {c: s(v) for c, v in bal["null_required"].items()},
        },
        "business": {"balance_move_over_300pct": s(bal["spike_over_300pct"])},
    }
    (args.out / "violations_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    shown = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    print(f"Wrote raw tables to {shown} (seed={args.seed}, as-of {today.isoformat()})")
    for name, df in tables.items():
        print(f"  {name + '.csv':<26} {len(df):>7,} rows")
    print("  violations_manifest.json")
    print("Planted defects:")
    print(f"  {'branches.duplicate_branch_id':<38} {DUPLICATE_BRANCH_ID}")
    for prefix, planted in (("accounts", acct), ("daily_balances", bal)):
        for kind, ids in planted.items():
            if isinstance(ids, dict):
                ids = [f"{k} ({c})" for c, ks in ids.items() for k in ks]
            more = f" ... (+{len(ids) - 5} more)" if len(ids) > 5 else ""
            print(f"  {prefix + '.' + kind:<38} {', '.join(ids[:5])}{more}")


if __name__ == "__main__":
    main()
