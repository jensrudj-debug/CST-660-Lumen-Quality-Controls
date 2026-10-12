"""Validate a dataset against its data contract.

Two kinds of check:

1. Data: the CSV must match the contract's schema (columns, types,
   nullability, primary key, patterns, allowed values, ranges, dates not in
   the future, foreign keys) and its freshness SLA.
2. Contract evolution: the contract file is compared with its version on
   origin/main (or --base-contract). A change the breaking_change_policy calls
   BREAKING must come with a major version bump. If there is no base version,
   this check is skipped with a note.

Every violation is printed as "[CONTRACT VIOLATION] <rule>: <detail>".

Exit codes: 0 no violations, 1 one or more violations, 2 the contract or data
could not be read.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "contracts" / "accounts.contract.yml"
DEFAULT_DATA = ROOT / "data" / "staged" / "accounts.csv"
DEFAULT_BASE_REF = "origin/main"

MAX_KEYS = 5
TYPE_PATTERNS = {"string": r"(?s).+", "decimal": r"-?\d+(\.\d+)?", "integer": r"-?\d+"}
DEFAULT_FORMATS = {"date": "%Y-%m-%d", "datetime": "%Y-%m-%dT%H:%M:%S"}


class Report:
    def __init__(self) -> None:
        self.violations: list[tuple[str, str]] = []

    def violation(self, rule: str, detail: str) -> None:
        self.violations.append((rule, detail))
        print(f"[CONTRACT VIOLATION] {rule}: {detail}")

    @staticmethod
    def note(message: str) -> None:
        print(f"[NOTE] {message}")


def sample(keys: pd.Series) -> str:
    keys = sorted(set(keys))
    more = f" (+{len(keys) - MAX_KEYS} more)" if len(keys) > MAX_KEYS else ""
    return f"[{', '.join(keys[:MAX_KEYS])}]{more}"


def parse_run_time(value: str | None) -> datetime:
    """A date means the end of that day; a datetime is used as given; default is now."""
    if value is None:
        return datetime.now().replace(microsecond=0)
    try:
        return datetime.strptime(value, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        return datetime.fromisoformat(value)


def parse_version(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in str(version).split("."))
    return major, minor, patch


# --- data checks -------------------------------------------------------------

def check_data(contract: dict, df: pd.DataFrame, data_path: Path, run_time: datetime,
               report: Report) -> None:
    columns = contract["schema"]["columns"]
    declared = [c["name"] for c in columns]
    for name in declared:
        if name not in df.columns:
            report.violation(f"schema.missing_column({name})",
                             "declared in the contract but absent from the data")
    for name in df.columns:
        if name not in declared:
            report.violation(f"schema.unexpected_column({name})",
                             "present in the data but not declared in the contract")

    pk = contract["schema"].get("primary_key", [])
    if pk and all(c in df.columns for c in pk):
        keys = df[pk].fillna("<null>").astype(str).agg("|".join, axis=1)
    else:
        keys = pd.Series([f"row {i + 2}" for i in range(len(df))], index=df.index)

    parsed: dict[str, pd.Series] = {}
    for spec in columns:
        name = spec["name"]
        if name not in df.columns:
            continue
        values = df[name]
        present = values.notna()

        if not spec.get("nullable", True) and (~present).any():
            report.violation(f"nullable({name})",
                             f"{(~present).sum()} null value(s) in a non-nullable column; "
                             f"keys {sample(keys[~present])}")

        kind = spec["type"]
        if kind in DEFAULT_FORMATS:
            fmt = spec.get("format", DEFAULT_FORMATS[kind])
            parsed[name] = pd.to_datetime(values, format=fmt, errors="coerce")
            valid = parsed[name].notna()
            expected = f"{kind} in format {fmt}"
        elif kind in TYPE_PATTERNS:
            valid = values.fillna("").str.fullmatch(TYPE_PATTERNS[kind])
            expected = kind
        else:
            report.violation(f"type({name})", f"contract declares unknown type '{kind}'")
            continue
        bad = present & ~valid
        if bad.any():
            examples = ", ".join(repr(v) for v in values[bad].unique()[:3])
            report.violation(f"type({name})",
                             f"{bad.sum()} value(s) are not {expected}, e.g. {examples}; "
                             f"keys {sample(keys[bad])}")
        ok = present & valid

        if "pattern" in spec:
            bad = ok & ~values.fillna("").str.fullmatch(spec["pattern"])
            if bad.any():
                report.violation(f"pattern({name})",
                                 f"{bad.sum()} value(s) do not match {spec['pattern']}; "
                                 f"keys {sample(keys[bad])}")

        if "allowed_values" in spec:
            allowed = [str(v) for v in spec["allowed_values"]]
            bad = ok & ~values.isin(allowed)
            if bad.any():
                found = ", ".join(sorted(values[bad].unique())[:5])
                report.violation(f"allowed_values({name})",
                                 f"{bad.sum()} value(s) outside {allowed}: {found}; "
                                 f"keys {sample(keys[bad])}")

        if "range" in spec:
            rng = spec["range"]
            if rng.get("applies_to") == "numeric_part":
                numbers = pd.to_numeric(values.str.extract(r"(\d+)", expand=False), errors="coerce")
                what = "numeric part"
            else:
                numbers = pd.to_numeric(values, errors="coerce")
                what = "value"
            bad = ok & (numbers.isna() | (numbers < rng.get("min", float("-inf")))
                        | (numbers > rng.get("max", float("inf"))))
            if bad.any():
                found = ", ".join(sorted(values[bad].unique())[:5])
                report.violation(f"range({name})",
                                 f"{bad.sum()} {what}(s) outside [{rng.get('min')}, "
                                 f"{rng.get('max')}]: {found}; keys {sample(keys[bad])}")

        if spec.get("not_in_future") and name in parsed:
            # The calendar day the run belongs to (run_time can be the
            # midnight that ends it).
            run_date = (run_time - timedelta(microseconds=1)).date()
            if kind == "datetime":
                bad = ok & (parsed[name] > run_time)
            else:
                bad = ok & (parsed[name] > pd.Timestamp(run_date))
            if bad.any():
                report.violation(f"not_in_future({name})",
                                 f"{bad.sum()} value(s) after the run date {run_date}; "
                                 f"keys {sample(keys[bad])}")

        if "foreign_key" in spec:
            fk = spec["foreign_key"]
            parent_path = data_path.parent / f"{fk['table']}.csv"
            if not parent_path.exists():
                report.note(f"foreign_key({name}): {parent_path.name} not found next to the "
                            f"data; skipped")
            else:
                parent = pd.read_csv(parent_path, dtype=str, usecols=[fk["column"]])
                bad = ok & ~values.isin(parent[fk["column"]])
                if bad.any():
                    report.violation(f"foreign_key({name})",
                                     f"{bad.sum()} value(s) have no match in "
                                     f"{fk['table']}.{fk['column']}; keys {sample(keys[bad])}")

    if pk and all(c in df.columns for c in pk):
        complete = df[pk].notna().all(axis=1)
        dup = complete & df.duplicated(pk, keep=False)
        if dup.any():
            report.violation(f"primary_key({', '.join(pk)})",
                             f"{dup.sum()} rows share {keys[dup].nunique()} key value(s); "
                             f"keys {sample(keys[dup])}")

    check_freshness(contract, parsed, run_time, report)


def check_freshness(contract: dict, parsed: dict[str, pd.Series], run_time: datetime,
                    report: Report) -> None:
    sla = contract.get("freshness")
    if not sla:
        return
    col, max_age = sla["column"], timedelta(hours=sla["max_age_hours"])
    latest = parsed[col].max() if col in parsed else pd.NaT
    if pd.isna(latest):
        report.violation(f"freshness({col})", "no valid timestamps to measure freshness")
        return
    age = run_time - latest.to_pydatetime()
    if age > max_age:
        report.violation(f"freshness({col})",
                         f"latest {col} {latest:%Y-%m-%dT%H:%M:%S} is "
                         f"{age.total_seconds() / 3600:.1f}h before run time "
                         f"{run_time:%Y-%m-%dT%H:%M:%S}; SLA is {sla['max_age_hours']}h")


# --- contract evolution -------------------------------------------------------

def diff_contracts(base: dict, new: dict) -> list[tuple[str, str]]:
    """List (change kind, description). Kinds use the policy's own wording."""
    changes = []
    base_cols = {c["name"]: c for c in base["schema"]["columns"]}
    new_cols = {c["name"]: c for c in new["schema"]["columns"]}

    for name in base_cols.keys() - new_cols.keys():
        # A rename looks like a removal plus an addition; both are breaking.
        changes.append(("remove a column", f"column '{name}' removed or renamed"))
    for name in new_cols.keys() - base_cols.keys():
        if new_cols[name].get("nullable", True):
            changes.append(("add a nullable column", f"nullable column '{name}' added"))
        else:
            changes.append(("add a non-nullable column", f"non-nullable column '{name}' added"))

    for name in sorted(base_cols.keys() & new_cols.keys()):
        old, cur = base_cols[name], new_cols[name]
        if old["type"] != cur["type"]:
            changes.append(("change a column's type",
                            f"'{name}' type changed {old['type']} -> {cur['type']}"))
        was, now = old.get("nullable", True), cur.get("nullable", True)
        if was != now:
            changes.append((f"change nullable from {str(was).lower()} to {str(now).lower()}",
                            f"'{name}' nullable changed {str(was).lower()} -> {str(now).lower()}"))
        old_vals = {str(v) for v in old.get("allowed_values", [])}
        new_vals = {str(v) for v in cur.get("allowed_values", [])}
        if "allowed_values" in old and "allowed_values" in cur:
            for v in sorted(old_vals - new_vals):
                changes.append(("remove an allowed enum value",
                                f"'{name}' allowed value '{v}' removed"))
            for v in sorted(new_vals - old_vals):
                changes.append(("add an allowed enum value",
                                f"'{name}' allowed value '{v}' added"))
        if old.get("description") != cur.get("description"):
            changes.append(("change a description", f"'{name}' description changed"))
    return changes


def policy_tier(policy: dict, kind: str) -> str | None:
    for tier in ("major", "minor", "patch"):
        if kind.lower() in (c.lower() for c in policy.get(tier, {}).get("changes", [])):
            return tier
    return None


def load_base(contract_path: Path, base_contract: Path | None, base_ref: str,
              report: Report) -> tuple[dict | None, str]:
    if base_contract is not None:
        return yaml.safe_load(base_contract.read_text()), str(base_contract)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=contract_path.parent,
                             capture_output=True, text=True, check=True).stdout.strip()
        rel = contract_path.resolve().relative_to(Path(top).resolve()).as_posix()
        text = subprocess.run(["git", "show", f"{base_ref}:{rel}"], cwd=top,
                              capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        report.note(f"no version of {contract_path.name} on {base_ref}; "
                    f"skipping breaking-change check")
        return None, base_ref
    return yaml.safe_load(text), f"{base_ref}:{rel}"


def check_evolution(contract: dict, contract_path: Path, base_contract: Path | None,
                    base_ref: str, report: Report) -> None:
    base, label = load_base(contract_path, base_contract, base_ref, report)
    if base is None:
        return
    old_v, new_v = base["contract"]["version"], contract["contract"]["version"]
    changes = diff_contracts(base, contract)
    if not changes:
        report.note(f"no schema changes against {label} (v{old_v})")
        return

    policy = contract.get("breaking_change_policy", {})
    major_bump = parse_version(new_v)[0] > parse_version(old_v)[0]
    for kind, description in changes:
        tier = policy_tier(policy, kind)
        if tier == "major" and not major_bump:
            report.violation("breaking_change",
                             f"{description} is BREAKING ({kind}) but version {old_v} -> "
                             f"{new_v} is not a major bump; requires a major version bump "
                             f"and consumer sign-off")
        elif tier is None:
            report.note(f"change not covered by breaking_change_policy: {description}")
        else:
            report.note(f"{tier} change against {label}: {description}")


# --- entry point --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--run-date", help="YYYY-MM-DD (end of that day) or ISO datetime; "
                                           "default now")
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF,
                        help="git ref holding the previous contract (default origin/main)")
    parser.add_argument("--base-contract", type=Path,
                        help="compare against this file instead of --base-ref")
    args = parser.parse_args(argv)

    try:
        contract = yaml.safe_load(args.contract.read_text())
        df = pd.read_csv(args.data, dtype=str, keep_default_na=False, na_values=[""])
    except (OSError, yaml.YAMLError, pd.errors.ParserError) as exc:
        print(f"[ERROR] could not read input: {exc}", file=sys.stderr)
        return 2

    run_time = parse_run_time(args.run_date)
    meta = contract["contract"]
    print(f"Validating {args.data} against contract '{meta['name']}' v{meta['version']} "
          f"(run time {run_time:%Y-%m-%dT%H:%M:%S})")

    report = Report()
    check_data(contract, df, args.data, run_time, report)
    check_evolution(contract, args.contract, args.base_contract, args.base_ref, report)

    n = len(report.violations)
    if n:
        print(f"FAILED: {n} contract violation(s) in {len(df):,} rows")
        return 1
    print(f"PASSED: 0 contract violations in {len(df):,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
