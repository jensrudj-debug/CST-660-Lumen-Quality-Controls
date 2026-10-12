"""Relationship layer: every foreign key resolves, and joins do not fan out.

The fan-out test guards against last quarter's incident: a duplicated
branch_id in the branch reference table silently multiplied balance rows and
overstated average balances in the CFO's deposits packet.
"""

import pytest

from checks import FOREIGN_KEYS, join_fanout, manifest_mismatch, orphan_keys, show, violation

pytestmark = pytest.mark.relationships

FK_IDS = [f"{child}.{col}->{parent}" for child, col, parent, _ in FOREIGN_KEYS]
FANOUT_RULE = "daily_balances -> accounts -> branches join must preserve row count"


def fk_rule(child, col, parent, parent_col):
    return f"foreign key {col} must resolve to {parent}.{parent_col}"


@pytest.mark.parametrize("fk", FOREIGN_KEYS, ids=FK_IDS)
def test_staged_foreign_keys_resolve(staged, fk):
    orphans = orphan_keys(staged, *fk)
    assert not orphans, violation(fk[0], fk_rule(*fk), orphans)


def test_staged_join_does_not_fan_out(staged):
    extra, accounts = join_fanout(staged)
    n = len(staged["daily_balances"])
    assert extra == 0, (
        f"daily_balances: {FANOUT_RULE}: expected {n:,} rows, got {n + extra:,} "
        f"(+{extra:,}); {len(accounts)} offending account_id(s): {show(accounts)}")


@pytest.mark.parametrize("fk", FOREIGN_KEYS, ids=FK_IDS)
def test_raw_orphans_match_manifest(raw, manifest, fk):
    found = orphan_keys(raw, *fk)
    expected = manifest["relationships"][f"{fk[0]}.{fk[1]}"]
    assert set(found) == set(expected), manifest_mismatch(fk[0], fk_rule(*fk), found, expected)


def test_raw_join_fanout_matches_manifest(raw, manifest):
    extra, accounts = join_fanout(raw)
    expected = manifest["join_fanout"]
    assert set(accounts) == set(expected["account_ids"]), manifest_mismatch(
        "daily_balances", FANOUT_RULE, accounts, expected["account_ids"])
    assert extra == expected["extra_rows"], (
        f"daily_balances: {FANOUT_RULE}: raw join added {extra:,} rows, manifest expects "
        f"{expected['extra_rows']:,}; offending account_id(s): {show(accounts)}")
