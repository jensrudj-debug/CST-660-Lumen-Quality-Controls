"""Primary-key layer: no key value appears on more than one row.

daily_balances has a composite key of (account_id, balance_date).
"""

import pytest

from checks import PRIMARY_KEYS, TABLES, duplicate_keys, manifest_mismatch, violation

pytestmark = pytest.mark.pk


def rule(table: str) -> str:
    return f"primary key ({', '.join(PRIMARY_KEYS[table])}) must be unique"


@pytest.mark.parametrize("table", TABLES)
def test_staged_primary_key_unique(staged, table):
    dups = duplicate_keys(staged[table], table)
    assert not dups, violation(table, rule(table), dups)


@pytest.mark.parametrize("table", TABLES)
def test_raw_duplicate_keys_match_manifest(raw, manifest, table):
    found = duplicate_keys(raw[table], table)
    expected = manifest["pk"][table]
    assert set(found) == set(expected), manifest_mismatch(table, rule(table), found, expected)
