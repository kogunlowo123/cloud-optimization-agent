# Contributing

Thanks for helping improve this project. This guide covers the workflow and the quality bar.

## Development setup

```bash
git clone <repository-url>
cd cloud-optimization-agent
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
```

## Checks

Every change must pass the gates CI enforces:

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy --strict
make cov         # pytest with a coverage gate of 80%
```

`make format` applies safe autofixes and formatting.

## Workflow

1. Open an issue for anything larger than a small fix so the design can be discussed first.
2. Branch from `main`: `feature/<short-name>` or `fix/<short-name>`.
3. Keep commits focused, with imperative subjects.
4. Add or update tests. Bug fixes need a regression test that fails without the fix.
5. Update `CHANGELOG.md` under **Unreleased** and any affected documentation.
6. Open a pull request describing the problem, the approach and how you verified it.

## Code standards

- Python 3.10+, fully type-annotated, `mypy --strict` clean.
- Docstrings explain behaviour, not restate names.
- Errors raised deliberately derive from `OptError`.
- Analysis is pure. The same inventory, billing, policy, prices and date give the same report.
- Analyzers propose with a `fraction` and know nothing about each other. Only the planner prices savings.
- Every threshold is a `Policy` field with a default and a test that shows it changes the outcome.
- Anything derived from inventory data that ends up in Markdown, CSV or a shell command goes through
  `md_cell`, `md_code`, `csv_safe` or `commands._quote`.
- Never send resource names, ids or tags to a language model.
- Tests are offline and deterministic. Use the builders in `tests/conftest.py`.

## Adding a rule

1. Write a function that takes a `Context` and returns recommendations built with `make_rec`. Return nothing when
   the data is missing, and count blind spots so the report can warn about them.
2. Choose the `fraction`, confidence, risk, effort and a list of checks a person should do before acting.
3. Register it in `OptimizationService.analyze` and add its category to `CATEGORY_ORDER` if it is new.
4. Test that it fires, that it stays quiet on similar healthy data, and that each policy threshold changes the
   outcome. Add a case to the planner tests if it overlaps another rule.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not file public issues for vulnerabilities.

## License

By contributing you agree that your contributions are licensed under the MIT License.
