"""Not-null layer: required fields are always populated."""

import pytest

from checks import TABLES, manifest_mismatch, null_violations, violation

pytestmark = pytest.mark.not_null


@pytest.mark.parametrize("table", TABLES)
def test_staged_required_fields_not_null(staged, table):
    found = null_violations(staged[table], table)
    assert not found, "; ".join(
        violation(table, f"{col} is required", keys) for col, keys in found.items())


@pytest.mark.parametrize("table", TABLES)
def test_raw_nulls_match_manifest(raw, manifest, table):
    found = null_violations(raw[table], table)
    expected = manifest["not_null"].get(table, {})
    for col in sorted(set(found) | set(expected)):
        assert set(found.get(col, [])) == set(expected.get(col, [])), manifest_mismatch(
            table, f"{col} is required", found.get(col, []), expected.get(col, []))
