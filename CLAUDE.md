# Lumen Community Bank: Data Quality Controls

Automated data quality controls for a synthetic core-banking dataset: a layered test suite, a producer-consumer data contract for the accounts table, and a GitHub Actions gate that blocks bad changes.

## Project rules

1. **Never run `git commit` or `git push` until the user explicitly says to in a separate message.** Approval to commit once does not carry over to later work.
2. **When a unit of work is finished, stop.** Summarize what changed, then wait for the next instruction.
3. **Use conventional commit messages**, for example `feat:`, `fix:`, `test:`, `ci:`, `docs:` or `chore:`.
4. **Run the tests before every commit** (`make test`, or `python -m pytest tests -v`).
5. **Once branch protection is enabled, never commit directly to `main`.** Always use a feature branch and a pull request.
6. **Never skip or weaken a failing test to make CI pass.** Fix the root cause instead.

## Layout

- `generator/`: synthetic data generator
- `data/raw/`, `data/staged/`: generated CSVs (git-ignored)
- `tests/`: pytest suite
- `contracts/`: YAML data contracts
- `scripts/`: staging and contract validation scripts
- `.github/workflows/`: CI/CD workflows

## Commands

`make generate`, `make stage`, `make test`, `make validate`, `make all`
