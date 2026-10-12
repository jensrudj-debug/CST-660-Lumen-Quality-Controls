"""Generate a synthetic core-banking dataset for Lumen Community Bank.

Writes four CSVs to data/raw/:
    branches.csv        42-branch reference table
    customers.csv       customer master
    accounts.csv        deposit accounts
    daily_balances.csv  one end-of-day balance per account per day

Defects are planted on purpose so the staging step and the test suite have
something to catch. Every planted defect is listed in PLANTED below and
printed at the end of a run.

Output is deterministic for a given seed, except that balance dates end on the
run date so the freshness SLA in the accounts contract can be checked.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"

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
    "daily_balances.unknown_account_id": 3,
    "daily_balances.duplicate_pk": 5,
    "daily_balances.null_required": 4,
    "daily_balances.spike_over_300pct": 6,
    "branches.duplicate_branch_id": 1,
}

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
            "branch_id": f"BR{i + 1:03d}",
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
    dup = branches.loc[branches["branch_id"] == "BR017"].copy()
    dup["branch_name"] = dup["branch_name"] + " (Relocated)"
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
    """Corrupt a few account rows. Returns the table and the affected ids."""
    accounts = accounts.astype({"customer_id": object, "branch_id": object,
                                "account_type": object, "open_date": object})
    pool = list(rng.permutation(len(accounts)))
    take = lambda k: [pool.pop() for _ in range(k)]  # noqa: E731
    planted: dict[str, list[str]] = {}

    idx = take(PLANTED["accounts.orphan_customer_id"])
    accounts.loc[idx, "customer_id"] = [f"C9{i:04d}" for i in range(len(idx))]
    planted["orphan_customer_id"] = accounts.loc[idx, "account_id"].tolist()

    idx = take(PLANTED["accounts.orphan_branch_id"])
    accounts.loc[idx, "branch_id"] = ["BR043", "BR099", "BR000", "BR143"][: len(idx)]
    planted["orphan_branch_id"] = accounts.loc[idx, "account_id"].tolist()

    idx = take(PLANTED["accounts.null_required"])
    for i, col in zip(idx, ["customer_id", "account_type", "open_date", "branch_id"]):
        accounts.loc[i, col] = None
    planted["null_required"] = accounts.loc[idx, "account_id"].tolist()

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
        "available_balance": (ledger - holds).ravel(),
    })


def plant_balance_defects(rng: np.random.Generator, balances: pd.DataFrame,
                          skip_accounts: set[str]) -> tuple[pd.DataFrame, dict]:
    balances = balances.astype({"ledger_balance": object, "available_balance": object})
    planted: dict[str, list[str]] = {}

    # Pick target accounts that are otherwise clean, so each defect has a
    # single cause.
    candidates = sorted(set(balances["account_id"]) - skip_accounts)
    chosen = list(rng.choice(candidates, size=PLANTED["daily_balances.spike_over_300pct"]
                             + PLANTED["daily_balances.null_required"]
                             + PLANTED["daily_balances.duplicate_pk"], replace=False))

    # Spikes: one day where the balance jumps to 5-8x the previous day, as if
    # an amount was keyed in cents instead of dollars or a decimal was lost.
    spike_ids = [chosen.pop() for _ in range(PLANTED["daily_balances.spike_over_300pct"])]
    for acct in spike_ids:
        rows = balances.index[balances["account_id"] == acct]
        i = rows[int(rng.integers(10, len(rows) - 1))]
        factor = float(rng.uniform(5, 8))
        balances.loc[i, "ledger_balance"] = round(balances.loc[i - 1, "ledger_balance"] * factor, 2)
        balances.loc[i, "available_balance"] = balances.loc[i, "ledger_balance"]
    planted["spike_over_300pct"] = spike_ids

    null_ids = [chosen.pop() for _ in range(PLANTED["daily_balances.null_required"])]
    for acct, col in zip(null_ids, ["ledger_balance", "balance_date", "ledger_balance", "account_id"]):
        rows = balances.index[balances["account_id"] == acct]
        balances.loc[rows[int(rng.integers(0, len(rows)))], col] = None
    planted["null_required"] = null_ids

    # Duplicate (account_id, balance_date): a reloaded day with a slightly
    # different figure.
    dup_ids = [chosen.pop() for _ in range(PLANTED["daily_balances.duplicate_pk"])]
    dup_rows = []
    for acct in dup_ids:
        rows = balances.index[balances["account_id"] == acct]
        row = balances.loc[rows[int(rng.integers(0, len(rows)))]].copy()
        row["ledger_balance"] = round(row["ledger_balance"] + 100.0, 2)
        dup_rows.append(row)
    planted["duplicate_pk"] = dup_ids

    # Balances for accounts that do not exist in accounts.csv.
    unknown = [f"A9{i:05d}" for i in range(PLANTED["daily_balances.unknown_account_id"])]
    template = balances[balances["account_id"] == candidates[0]]
    ghost_rows = [template.assign(account_id=acct) for acct in unknown]
    planted["unknown_account_id"] = unknown

    out = pd.concat([balances, pd.DataFrame(dup_rows), *ghost_rows], ignore_index=True)
    return out, planted


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
    accounts, acct_planted = plant_account_defects(rng, accounts)

    balances = make_balances(rng, accounts, today)
    bad_accounts = {a for ids in acct_planted.values() for a in ids}
    balances, bal_planted = plant_balance_defects(rng, balances, bad_accounts)

    tables = {"branches": branches, "customers": customers,
              "accounts": accounts, "daily_balances": balances}
    for name, df in tables.items():
        df.to_csv(args.out / f"{name}.csv", index=False)

    print(f"Wrote raw tables to {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}"
          f" (seed={args.seed}, as-of {today.isoformat()})")
    for name, df in tables.items():
        print(f"  {name + '.csv':<20} {len(df):>7,} rows")
    print("Planted defects:")
    print(f"  {'branches.duplicate_branch_id':<36} BR017")
    for prefix, planted in (("accounts", acct_planted), ("daily_balances", bal_planted)):
        for kind, ids in planted.items():
            print(f"  {prefix + '.' + kind:<36} {', '.join(ids)}")


if __name__ == "__main__":
    main()
