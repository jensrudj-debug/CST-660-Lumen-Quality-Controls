# Lumen Community Bank: Data Quality Controls

Automated data quality controls for Lumen Community Bank's core-banking data:
a layered test suite, a producer-consumer data contract for the accounts
table, and a GitHub Actions gate that blocks bad changes before they reach
staging.

## 1. Background

Last quarter a deposits-reporting model shipped with a silent **join fan-out**
against the branch reference table. One branch_id appeared on more than one
row, so every balance joined to that branch was counted more than once. Average
customer balances in the CFO's weekly deposits packet were overstated for
**eleven days**, until a branch manager noticed her numbers did not reconcile.

Nothing failed. The query ran, the numbers looked plausible, and no check
compared the joined row count with the source. This repository turns that
failure class into automated controls that run on every change:

- **Primary-key tests** on every table, including the branch reference table,
  catch the duplicate key that causes a fan-out.
- **A join fan-out test** asserts that `daily_balances -> accounts -> branches`
  returns exactly as many rows as `daily_balances`, catching the symptom even
  if the cause is new.
- **A data contract** for the accounts table pins down what consumers can rely
  on and makes breaking changes visible.
- **A CI gate** runs all of it on every pull request and blocks the merge on
  any failure, so a bad change cannot reach the packet unnoticed.

The data is synthetic. The generator plants defects on purpose (orphaned keys,
duplicate keys, nulls, wrong types, balance spikes and a duplicated branch_id)
so every control can be shown catching a real problem.

## 2. Architecture

```
generator/generate_data.py          scripts/stage.py                 tests/  +  scripts/validate_contract.py
          │                                │                                       │
          ▼                                ▼                                       ▼
   data/raw/*.csv  ───────────────►  data/staged/*.csv  ─────────────►  pytest layers + contract validation
   violations_manifest.json          data/staged/quarantine/*.csv                  │
                                                                                   ▼
                                                              GitHub Actions: PR gate ─► merge ─► staging promotion
```

| Component | Location | Role |
|---|---|---|
| Raw data | `data/raw/` | Synthetic branches (42), customers, accounts and 30 days of daily balances, with planted defects. `violations_manifest.json` lists every planted defect by key. |
| Staging | `scripts/stage.py` → `data/staged/` | Copies clean rows of `accounts` and `daily_balances` to `data/staged/`. |
| Quarantine | `data/staged/quarantine/` | Rejected rows, each with a `quarantine_reason` (orphaned keys, duplicate keys, nulls, invalid types, unknown accounts, balance moves over 300%). Balances whose account was quarantined are tagged `parent_account_quarantined`, so downstream fallout is never mistaken for a separate problem. |
| Reference data | `data/staged/branches.csv` | Passed through **untouched**. A trusted reference table is never deduplicated in staging: a duplicate branch_id must fail loudly in the tests, not be hidden. |
| Test layers | `tests/` | Schema, primary keys, relationships, not-null and business rules, each run against staged data (must be clean) and raw data (must find exactly the manifest's defects). |
| Contract | `contracts/accounts.contract.yml` | Machine-readable agreement for the accounts table, checked by `scripts/validate_contract.py`. |
| CI gate | `.github/workflows/ci.yml` | Runs `tests` and `contract-validation` on every pull request; branch protection requires both. |
| Staging promotion | `.github/workflows/ci.yml` | On merge to main, uploads `data/staged/` to the `staging` environment and tags the commit `staging-<short sha>`. |
| Rollback | `.github/workflows/rollback.yml` | Manually redeploys a previous `staging-*` tag. |

## 3. Running locally

Requires Python 3.11+ and `make`.

```bash
pip install -r requirements.txt

make generate   # write synthetic raw data and the violations manifest to data/raw/
make stage      # quarantine bad rows, write clean tables to data/staged/
make test       # run the full pytest suite
make validate   # validate data/staged/accounts.csv against the contract
make all        # all four, in order
```

Useful variations:

```bash
python -m pytest -m relationships -v          # run one layer (schema, pk, relationships, not_null, business, contract)
python scripts/validate_contract.py --data data/raw/accounts.csv   # see every contract violation in raw data
python scripts/validate_contract.py --run-date 2026-10-11          # validate as of a given day
```

The validator exits `0` with no violations, `1` with any violation and `2` if
the contract or data cannot be read.

## 4. Test layers

Every layer runs two kinds of test:

- **Staged data passes clean.** This is the gate.
- **The same check against raw data finds exactly the keys in
  `violations_manifest.json`.** This proves the check is not passing
  vacuously: if it stopped detecting anything, the raw test would fail.

Every failure message names the table, the rule and up to five offending keys.

| Layer (marker) | What it checks | What a failure indicates about pipeline health |
|---|---|---|
| Schema (`schema`) | Every table has the expected columns, in order, and every value matches its declared type (ISO dates, plain decimals). | The producer changed the file layout or format, e.g. a renamed column or a spreadsheet export writing `03/15/2019` or `1,234.56`. Everything downstream that parses these columns is at risk. |
| Primary keys (`pk`) | Key uniqueness on all four tables, including the composite `(account_id, balance_date)` key. | A load appended rows instead of upserting, or a reference table was edited by hand. Any join on that key will fan out. **This is the root cause of last quarter's incident.** |
| Relationships (`relationships`) | Every foreign key resolves, and `daily_balances -> accounts -> branches` preserves row count. | Orphans mean a parent table is incomplete or loaded out of order. A changed join row count means a duplicated join key is multiplying rows: **the incident's symptom**, which overstates every sum and average built on the join. |
| Not null (`not_null`) | Required fields are always populated. | An upstream source stopped sending a field, or a mapping broke. Rows will silently drop out of filters and group-bys. |
| Business rules (`business`) | No account's ledger balance moves more than 300% day over day (`abs(today - yesterday) / abs(yesterday) > 3.0`; a move off a zero balance is undefined and not flagged). No more than 5% of accounts lose rows to quarantine, from either the accounts or the balances file. | A spike usually means a units or keying error (cents loaded as dollars, a lost decimal). A quarantine rate over 5% means the feed itself is broken, not a handful of rows, and staging must not quietly discard it. |
| Contract (`contract`) | Unit tests for the validator: clean pass, type mismatch, null in a non-nullable column, out-of-range values, stale freshness, duplicate key, breaking change with and without a major version bump. | The validator itself is broken and the contract can no longer be trusted to block bad data. |

**Symptom versus root cause.** Failures are designed to be read together. When
the fan-out test fails, the primary-key layer says *why*. When balances are
quarantined as `parent_account_quarantined`, the accounts quarantine holds the
real cause. When the same bad value fails both `range` and `foreign_key` in the
contract, it is one defect seen two ways. Fix the earliest failing layer first.

## 5. Data contract

`contracts/accounts.contract.yml` (version **1.0.0**) is the agreement between
the **producer**, Core Banking Data Engineering, and the **consumer**, Finance
Analytics (CFO weekly deposits packet). It specifies:

- **Schema:** all nine columns with type, nullability and primary key
  (`account_id`), plus ID formats and foreign keys to customers and branches.
- **Allowed values and ranges:** `account_type` in `CHECKING, SAVINGS,
  MONEY_MARKET, CD`; `status` in `OPEN, DORMANT, CLOSED`; `branch_id` numbered
  1–42 (`BR001`–`BR042`); `currency` is `USD`; `interest_rate` between 0 and
  10; `open_date` not in the future.
- **Freshness SLA:** the most recent `updated_at` must be within 24 hours of
  the run time.

`scripts/validate_contract.py` checks the staged accounts data against all of
this, and also compares the contract file with its version on `origin/main`.
Each violation prints as `[CONTRACT VIOLATION] <rule>: <detail>`.

### Breaking-change policy

The contract follows semantic versioning:

| Change | Bump | Requires |
|---|---|---|
| Remove or rename a column, change a column's type, change `nullable` from true to false, remove an allowed enum value | **Major** (BREAKING) | Major version bump **and** consumer sign-off |
| Add a nullable column, add an allowed enum value | Minor | Minor version bump |
| Change a description | Patch | Patch version bump |

The validator fails any pull request that makes a BREAKING change without a
major version bump. Consumer sign-off is a review on the pull request, not
something the script can check.

## 6. CI/CD

**On every pull request to `main`**, `.github/workflows/ci.yml` runs two jobs
on a fresh runner. Each generates and stages the data from scratch:

- **`tests`**: the full pytest suite.
- **`contract-validation`**: `scripts/validate_contract.py`, including the
  breaking-change comparison against `origin/main`.

**What blocks a merge.** Branch protection on `main` requires:

- a pull request (no direct pushes, administrators included),
- both `tests` and `contract-validation` to pass,
- the branch to be up to date with `main` before merging.

A failure in either job, or a branch behind `main`, disables the merge button.

**On merge to `main`** both jobs run again on the merged commit. If both pass,
**`promote-staging`**:

1. regenerates and stages the data,
2. uploads `data/staged/` as the artifact `staging-data-<sha>` to the
   `staging` environment,
3. tags the commit `staging-<short sha>`. Each tag is a known-good restore
   point for the rollback runbook.

If either check fails on `main`, nothing is promoted and staging keeps its last
good version. Promotions and rollbacks share a concurrency lock, so they never
deploy at the same time.

## 7. Rollback Runbook

Use this when a promotion to staging is bad: the checks passed, but the data
or its consumers are wrong.

1. **Recognize a bad promotion.** Typical signals:
   - Finance Analytics or a branch manager reports figures that do not
     reconcile (the way last quarter's incident was found).
   - Totals or averages in the deposits packet move sharply with no business
     reason, or row counts in the staged artifact change unexpectedly.
   - A later CI run on `main` fails a check that passed at promotion, e.g.
     the quarantine rate approaching 5%.

   Note the time it was noticed and the first `staging-*` tag that looks
   wrong. Tell Finance Analytics to hold the packet now (step 6), before the
   fix.

2. **Find the last good `staging-*` tag.** List restore points, newest first:

   ```bash
   git fetch --tags
   git tag -l 'staging-*' --sort=-creatordate --format='%(refname:short)  %(creatordate:iso)  %(subject)'
   ```

   Pick the newest tag **before** the bad promotion. To see what changed
   between it and the bad one:

   ```bash
   git log --oneline <good-tag>..<bad-tag>
   ```

3. **Run the rollback workflow with that tag.**

   ```bash
   gh workflow run rollback.yml -f ref=staging-<sha>
   gh run watch "$(gh run list --workflow rollback.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

   Or use **Actions → Rollback staging → Run workflow** in the GitHub UI. The
   workflow checks that the tag exists, reruns the tests and contract
   validation at that tag, and only then redeploys its staged data to the
   `staging` environment. The contract is compared with itself at that tag,
   so restoring an older contract is not blocked as a breaking change.

4. **Revert the offending merge commit through a pull request.** Rollback
   restores staging; `main` still contains the bad change and would promote it
   again on the next merge. `main` is protected, so the revert goes through a
   pull request like any other change:

   ```bash
   git fetch origin
   git switch -c revert/<short-sha> origin/main
   git revert -m 1 <merge-commit-sha>   # use `git revert <sha>` for a squash merge
   git push -u origin revert/<short-sha>
   gh pr create --base main --title "revert: <original PR title>" \
     --body "Reverts #<PR>. Staging rolled back to staging-<sha>. Root cause: <one line>."
   ```

   Merge it once `tests` and `contract-validation` pass. The merge promotes
   a new `staging-*` tag, which becomes the current known-good state.

5. **Verify the rollback.**
   - The rollback run shows `rollback-tests`, `rollback-contract-validation`
     and `redeploy-staging` all succeeded.
   - Its artifact `staging-data-rollback-staging-<sha>-<run id>` is in the
     staging environment. To inspect it locally:

     ```bash
     gh run download <run-id> -n staging-data-rollback-staging-<sha>-<run-id> -D rollback-check
     python scripts/validate_contract.py --data rollback-check/accounts.csv
     ```

   - Finance Analytics confirms the packet figures now reconcile to branch
     totals.
   - After the revert PR merges, the CI run on `main` is green and has
     pushed a new `staging-*` tag.

6. **Notify.**

   | Who | When | What |
   |---|---|---|
   | Finance Analytics (consumer; owner of the CFO weekly deposits packet) | When the problem is recognized, and again when verified | Hold the packet; then confirm the restored version and which figures were affected. |
   | Director of Data Engineering | When the rollback starts and when it is verified | Impact, time window, tag restored, link to the revert PR. |
   | Core Banking Data Engineering (producer) | During diagnosis | The root cause, if it is upstream data or a contract change. |
   | Author and reviewer of the offending PR | When the revert PR is opened | Link to the revert and the failing evidence, so the fix lands with a test that would have caught it. |

   Finish with a short write-up linking the bad tag, the rollback run, the
   revert PR and the new control (test or contract rule) that prevents a
   repeat.

## 8. Evidence

### Failing PR: a deliberate contract break blocked by the gate

<!-- PR link, the failing check, and the violation output -->

### Passing PR: the fix merges and promotes to staging

<!-- PR link, green checks, merge, and the staging-* tag created -->
