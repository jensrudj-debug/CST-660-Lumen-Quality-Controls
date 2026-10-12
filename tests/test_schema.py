"""Schema layer: every table has the expected columns, in order, with values of the declared type."""

import pytest

from checks import SCHEMAS, TABLES, manifest_mismatch, type_violations, violation

pytestmark = pytest.mark.schema


@pytest.mark.parametrize("table", TABLES)
def test_staged_columns_match_schema(staged, table):
    actual, expected = list(staged[table].columns), list(SCHEMAS[table])
    missing = [c for c in expected if c not in actual]
    unexpected = [c for c in actual if c not in expected]
    assert actual == expected, (
        f"{table}: column layout: expected {expected}, got {actual}; "
        f"missing: {missing}; unexpected: {unexpected}")


@pytest.mark.parametrize("table", TABLES)
def test_staged_values_match_declared_types(staged, table):
    found = type_violations(staged[table], table)
    assert not found, "; ".join(
        violation(table, f"type of {col} must be {SCHEMAS[table][col]}", keys)
        for col, keys in found.items())


@pytest.mark.parametrize("table", TABLES)
def test_raw_type_violations_match_manifest(raw, manifest, table):
    assert list(raw[table].columns) == list(SCHEMAS[table]), (
        f"{table}: column layout: raw columns {list(raw[table].columns)} "
        f"do not match schema {list(SCHEMAS[table])}")
    found = type_violations(raw[table], table)
    expected = manifest["schema"].get(table, {})
    for col in sorted(set(found) | set(expected)):
        assert set(found.get(col, [])) == set(expected.get(col, [])), manifest_mismatch(
            table, f"type of {col} must be {SCHEMAS[table][col]}",
            found.get(col, []), expected.get(col, []))
