#!/usr/bin/env bash
# Live demo: the CI gate catches a fan-out defect, and the rollback recovers
# from a bad promotion the gate could not catch.
#
# Run from Git Bash in a separate terminal:
#     bash scripts/demo.sh            full demo (pushes, opens and merges PRs)
#     bash scripts/demo.sh --check    rehearsal: local steps only, no git or GitHub
#
# Press Enter to advance each step; Ctrl-C stops at any point (see the
# recovery note printed on exit).
#
#   1. Reintroduce duplicate branch rows in the generator; show inflated totals
#      and failing tests locally
#   2. Commit, push and open a PR; watch CI go red
#   3. Fix the generator; tests pass locally; push; watch CI go green
#   4. Merge; watch main promote to staging and tag the last good release
#   5. Introduce a defect the gate cannot catch (balances in cents); merge it
#      and watch it get promoted
#   6. Recognize the bad promotion; roll staging back to the last good tag
#   7. Verify the redeployed staging artifact
#   8. Revert the bad merge through a PR so main is clean again

set -euo pipefail
cd "$(dirname "$0")/.."

# Never open a pager: the demo runs unattended between Enter presses, and a
# pager (less) would sit at "(END)" waiting for "q" and swallow the keypresses.
export GIT_PAGER=cat GH_PAGER=cat PAGER=cat

REPO="jensrudj-debug/CST-660-Lumen-Quality-Controls"
CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true
STAMP=$(date +%H%M%S)
TMP=$(mktemp -d)
GEN=generator/generate_data.py

bold=$'\e[1m'; red=$'\e[31m'; green=$'\e[32m'; yellow=$'\e[33m'; cyan=$'\e[36m'; off=$'\e[0m'

step()  { echo; echo "${cyan}${bold}==== $* ====${off}"; }
say()   { echo "${bold}$*${off}"; }
ok()    { echo "${green}$*${off}"; }
bad()   { echo "${red}$*${off}"; }
pause() { echo; read -rp "${yellow}>> Press Enter to $*${off} "; }
run()   { echo "${yellow}\$ $*${off}" >&2; "$@"; }

cleanup() {
    local code=$?
    rm -rf "$TMP"
    if [[ $code -ne 0 ]]; then
        echo
        bad "Demo stopped (exit $code). To get back to a clean state:"
        echo "  git restore $GEN; git switch main; git pull"
        echo "  Close any open demo PRs:  gh pr list"
    fi
}
trap cleanup EXIT

# --- helpers -------------------------------------------------------------------

# Edit the generator in place. Python keeps the file's line endings intact.
patch_generator() {
    python - "$1" <<'EOF'
import pathlib, sys
path = pathlib.Path("generator/generate_data.py")
src = path.read_text()
if sys.argv[1] == "dup-branches":
    old = '        raise ValueError("branches.branch_id must be unique")\n    return branches\n'
    new = ('        raise ValueError("branches.branch_id must be unique")\n'
           '    # Branch renames from the regional feed, loaded as new rows.\n'
           '    renamed = branches[branches["branch_id"].isin(["BR005", "BR017", "BR031"])].copy()\n'
           '    renamed["branch_name"] = "Lumen Community Bank " + renamed["city"]\n'
           '    branches = pd.concat([branches, renamed], ignore_index=True)\n'
           '    return branches\n')
elif sys.argv[1] == "cents":
    old = '    ledger = np.round(walk, 2)\n'
    new = '    ledger = np.round(walk * 100, 2)  # core banking now reports minor units\n'
else:
    sys.exit(f"unknown patch {sys.argv[1]}")
if old not in src:
    sys.exit(f"patch '{sys.argv[1]}': target text not found in {path}")
path.write_text(src.replace(old, new, 1))
EOF
}

# What the CFO packet would report: balances joined to branches, latest day.
packet_totals() {
    python - "$1" <<'EOF'
import sys
import pandas as pd
d = sys.argv[1]
bal = pd.read_csv(f"{d}/daily_balances.csv")
acc = pd.read_csv(f"{d}/accounts.csv")
br = pd.read_csv(f"{d}/branches.csv")
day = bal[bal["balance_date"] == bal["balance_date"].max()].merge(
    acc[["account_id", "branch_id"]], on="account_id")
packet = day.merge(br[["branch_id", "branch_name"]], on="branch_id")
true_total, shown = day["ledger_balance"].sum(), packet["ledger_balance"].sum()
dups = br["branch_id"][br["branch_id"].duplicated()].unique().tolist()
print(f"  branch rows: {len(br)}   duplicated branch_ids: {dups or 'none'}")
print(f"  rows: {len(day):,} account balances -> {len(packet):,} after the branch join")
print(f"  total deposits (sum of balances):   ${true_total:>18,.2f}")
print(f"  total deposits (CFO packet):        ${shown:>18,.2f}   ({shown / true_total - 1:+.1%} vs balances)")
print(f"  average balance per account:        ${shown / packet['account_id'].nunique():>18,.2f}")
EOF
}

local_build() { make generate stage > /dev/null; }

local_tests() {
    echo "${yellow}\$ make test${off}"
    python -m pytest tests -q -p no:cacheprovider 2>&1 \
        | grep -E "^FAILED|^E +AssertionError: |[0-9]+ (passed|failed).* in [0-9.]+s" || true
}

# Wait for the CI run on the commit just pushed (not an earlier one), watch it,
# then show the PR's checks and merge state.
watch_pr() {
    local pr=$1 sha id=""
    sha=$(git rev-parse HEAD)
    say "Waiting for CI on PR #$pr at ${sha:0:7} ..."
    until [[ -n "$id" ]]; do
        sleep 5
        id=$(gh run list --workflow ci.yml --event pull_request --commit "$sha" \
             --json databaseId --jq '.[0].databaseId // empty')
    done
    gh run watch "$id" --interval 10 > /dev/null 2>&1 || true
    sleep 3
    gh pr checks "$pr" || true
    gh pr view "$pr" --json mergeStateStatus,url \
        --jq '"merge state: \(.mergeStateStatus)   \(.url)"'
}

# Wait for the CI run on a commit pushed to main, watch it, return the tag.
watch_main() {
    local sha=$1 id=""
    say "Waiting for CI on main at ${sha:0:7} ..."
    until [[ -n "$id" ]]; do
        sleep 5
        id=$(gh run list --workflow ci.yml --branch main --event push --commit "$sha" \
             --json databaseId --jq '.[0].databaseId // empty')
    done
    gh run watch "$id" --interval 10 || true
    gh run view "$id" --json jobs --jq '.jobs[] | "  \(.name): \(.conclusion)"'
    git fetch --tags -q
}

open_pr() {
    local title=$1 body=$2
    gh pr create --base main --title "$title" --body "$body" | tail -1
}

merge_pr() {
    local pr=$1
    run gh pr merge "$pr" --merge >&2
    git switch -q main
    git pull -q --ff-only
    gh pr view "$pr" --json mergeCommit --jq '.mergeCommit.oid'
}

# --- preflight -------------------------------------------------------------------

step "Preflight"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    bad "Working tree has uncommitted changes. Commit or stash them first."; exit 1
fi
if ! $CHECK_ONLY; then
    gh auth status > /dev/null 2>&1 || { bad "gh is not logged in"; exit 1; }
    if [[ -n "$(gh pr list --state open --json number --jq '.[].number')" ]]; then
        bad "There are open PRs. Merge or close them first so the demo branches stay up to date."
        gh pr list; exit 1
    fi
    run git switch -q main
    run git pull -q --ff-only
fi
ok "Ready on $(git branch --show-current) at $(git rev-parse --short HEAD)."
$CHECK_ONLY && say "Rehearsal mode: no commits, pushes or PRs."

# --- 1. reintroduce the fan-out defect ------------------------------------------

pause "reintroduce the duplicate-branch defect in the generator"
step "1. Branch renames loaded as new rows"
patch_generator dup-branches
run git --no-pager diff --stat
git --no-pager diff -U1 "$GEN" | grep -E '^[+-] ' || true
say "Regenerate and stage the data, then compute what the CFO packet would show:"
local_build
packet_totals data/staged
pause "run the test suite locally"
local_tests

# --- 2. push it and watch CI catch it -------------------------------------------

if $CHECK_ONLY; then
    say "(rehearsal: skipping commit, push and PR)"
else
    pause "commit, push and open a pull request"
    step "2. Push the defect and let CI catch it"
    BR1="demo/regional-branch-feed-$STAMP"
    run git switch -q -c "$BR1"
    run git commit -q -am "feat: load branch renames from the regional feed"
    run git push -q -u origin "$BR1"
    PR1=$(open_pr "feat: load branch renames from the regional feed" \
        "Live demo: branch renames loaded as new rows. Expected to FAIL the tests check (duplicate branch_id and join fan-out).")
    PR1=${PR1##*/}
    watch_pr "$PR1"
    bad "The gate blocked the merge: duplicate branch_id and a join fan-out."
fi

# --- 3. fix it -------------------------------------------------------------------

pause "fix the generator"
step "3. Fix the root cause at the source"
if $CHECK_ONLY; then run git restore "$GEN"; else run git checkout origin/main -- "$GEN"; fi
local_build
packet_totals data/staged
pause "run the test suite locally"
local_tests

if ! $CHECK_ONLY; then
    pause "commit and push the fix"
    run git commit -q -am "fix: keep one row per branch_id in the branch reference table"
    run git push -q
    watch_pr "$PR1"
    ok "Both checks are green; the PR can merge."

    # --- 4. merge and promote ------------------------------------------------------

    pause "merge the PR and watch the promotion to staging"
    step "4. Merge and promote"
    SHA=$(merge_pr "$PR1")
    watch_main "$SHA"
    GOOD_TAG="staging-${SHA:0:7}"
    ok "Promoted to staging as $GOOD_TAG (last known-good release)."
    local_build
    packet_totals data/staged
fi

# --- 5. a defect the gate cannot catch -------------------------------------------

pause "introduce a defect the checks cannot see"
step "5. Balances loaded in cents instead of dollars"
patch_generator cents
git --no-pager diff -U0 "$GEN" | grep -E '^[+-] ' || true
local_build
packet_totals data/staged
pause "run the test suite locally"
local_tests
say "Every balance is 100x too large, but day-over-day moves are unchanged, so the"
say "300% rule cannot see it, and the contract only covers the accounts table."

if $CHECK_ONLY; then
    run git restore "$GEN"
    local_build
    ok "Rehearsal complete. Generator restored and data regenerated."
    exit 0
fi

pause "commit, push and merge it (it will pass CI)"
BR2="demo/minor-units-$STAMP"
run git switch -q -c "$BR2"
run git commit -q -am "feat: report balances in minor units"
run git push -q -u origin "$BR2"
PR2=$(open_pr "feat: report balances in minor units" \
    "Live demo: balances loaded in cents. Passes CI, but inflates every balance 100x; used to demonstrate the rollback.")
PR2=${PR2##*/}
watch_pr "$PR2"
BAD_SHA=$(merge_pr "$PR2")
watch_main "$BAD_SHA"
BAD_TAG="staging-${BAD_SHA:0:7}"
bad "Promoted to staging as $BAD_TAG. Staging now reports deposits 100x too high."

# --- 6. roll back ------------------------------------------------------------------

pause "follow the rollback runbook: find the last good tag"
step "6. Roll staging back"
run git --no-pager tag -l 'staging-*' --sort=-creatordate --format='%(refname:short)  %(creatordate:iso)  %(subject)'
say "Bad release: $BAD_TAG    Last good release: $GOOD_TAG"
pause "run the rollback workflow with $GOOD_TAG"
BEFORE=$(gh run list --workflow rollback.yml --limit 1 --json databaseId --jq '.[0].databaseId // 0')
run gh workflow run rollback.yml -f ref="$GOOD_TAG"
RB=""
until [[ -n "$RB" && "$RB" != "$BEFORE" ]]; do
    sleep 5
    RB=$(gh run list --workflow rollback.yml --limit 1 --json databaseId --jq '.[0].databaseId // empty')
done
gh run watch "$RB" --interval 10 || true
gh run view "$RB" --json jobs,url --jq '.url, (.jobs[] | "  \(.name): \(.conclusion)")'

# --- 7. verify -------------------------------------------------------------------

pause "verify the redeployed staging artifact"
step "7. Verify the rollback"
ART=$(gh api "repos/$REPO/actions/runs/$RB/artifacts" --jq '.artifacts[0].name')
run gh run download "$RB" -n "$ART" -D "$TMP/staging"
python scripts/validate_contract.py --data "$TMP/staging/accounts.csv" --base-ref HEAD | tail -1 || true
packet_totals "$TMP/staging"
ok "Staging is back on $GOOD_TAG with correct balances."

# --- 8. revert on main ---------------------------------------------------------------

pause "revert the bad merge on main through a pull request"
step "8. Revert the bad merge"
say "Staging is restored, but main still contains the bad change and would promote"
say "it again on the next merge."
BR3="revert/minor-units-$STAMP"
run git switch -q -c "$BR3"
run git revert --no-edit -m 1 "$BAD_SHA"
run git push -q -u origin "$BR3"
PR3=$(open_pr "revert: report balances in minor units" \
    "Reverts #$PR2. Staging was rolled back to $GOOD_TAG (rollback run $RB). Root cause: balances loaded in cents.")
PR3=${PR3##*/}
watch_pr "$PR3"
pause "merge the revert"
FINAL=$(merge_pr "$PR3")
watch_main "$FINAL"
ok "main is clean and promoted as staging-${FINAL:0:7}."

step "Demo complete"
echo "  Caught by the gate:  PR #$PR1 (red, fixed, merged as $GOOD_TAG)"
echo "  Bad promotion:       PR #$PR2 ($BAD_TAG)"
echo "  Rollback run:        https://github.com/$REPO/actions/runs/$RB"
echo "  Revert:              PR #$PR3 (staging-${FINAL:0:7})"
