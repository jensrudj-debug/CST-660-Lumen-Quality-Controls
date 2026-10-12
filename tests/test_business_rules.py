"""Business-rule layer: daily balance volatility, plus a guard on the quarantine rate."""

import pandas as pd
import pytest

from checks import balance_spikes, manifest_mismatch, show, violation

pytestmark = pytest.mark.business

SPIKE_RULE = "ledger_balance must not move more than 300% day over day"
MAX_QUARANTINE_RATE = 0.05


def test_staged_no_balance_move_over_300pct(staged):
    spikes = balance_spikes(staged["daily_balances"])
    assert not spikes, violation("daily_balances", SPIKE_RULE, spikes)


def test_raw_balance_spikes_match_manifest(raw, manifest):
    found = balance_spikes(raw["daily_balances"])
    expected = manifest["business"]["balance_move_over_300pct"]
    assert set(found) == set(expected), manifest_mismatch(
        "daily_balances", SPIKE_RULE, found, expected)


@pytest.mark.parametrize("ledger, flagged", [
    (["100", "400"], False),    # exactly 300% is the limit, not a breach
    (["100", "400.01"], True),  # just over 300%
    (["100", "24.99"], False),  # a fall can be at most 100%
    (["-100", "250"], True),    # 350% swing out of an overdraft
    (["0", "5000"], False),     # from zero: undefined percentage, not flagged
    (["0", "0"], False),        # no move
    (["0", "100", "500"], True),  # after funding, the next move is still measured
], ids=["at-300pct", "over-300pct", "drop", "overdraft", "from-zero", "zero-zero", "after-zero"])
def test_spike_rule_edge_cases(ledger, flagged):
    dates = [f"2026-01-{d:02d}" for d in range(1, len(ledger) + 1)]
    df = pd.DataFrame({"account_id": "A1", "balance_date": dates, "ledger_balance": ledger})
    assert bool(balance_spikes(df)) == flagged, (
        f"daily_balances: {SPIKE_RULE}: series {ledger} expected flagged={flagged}")


def assert_quarantine_rate(table: str, held: set, raw_accounts: set) -> None:
    held = sorted(held & raw_accounts)  # ids not in accounts at all are the FK layer's concern
    rate = len(held) / len(raw_accounts)
    assert rate <= MAX_QUARANTINE_RATE, violation(
        table, f"share of accounts quarantined must be <= {MAX_QUARANTINE_RATE:.0%}, "
        f"got {rate:.1%} ({len(held)} of {len(raw_accounts)})", held)


# A mass failure upstream must not disappear into the quarantine folder. If
# more than 5% of accounts lose data to quarantine, the problem is the feed,
# not a handful of bad rows, and the pipeline should stop. Both quarantine
# files are guarded: a feed-wide balances fault (say, a switch from dollars to
# cents) would quarantine balance rows for every account while leaving the
# accounts table untouched.

def test_account_quarantine_rate_below_5pct(raw, quarantine):
    """Accounts rejected outright by staging."""
    assert_quarantine_rate("accounts", set(quarantine["accounts"]["account_id"].dropna()),
                           set(raw["accounts"]["account_id"].dropna()))


def test_balance_quarantine_rate_below_5pct(raw, quarantine):
    """Accounts with at least one daily balance row quarantined."""
    assert_quarantine_rate("daily_balances",
                           set(quarantine["daily_balances"]["account_id"].dropna()),
                           set(raw["accounts"]["account_id"].dropna()))
