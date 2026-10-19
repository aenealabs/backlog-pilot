# Contributing to Backlog Pilot

Thank you for your interest in contributing to Backlog Pilot, part of the Mission
Control suite.

## Developer Certificate of Origin (DCO)

By contributing, you confirm that your contribution is your own work and that you have the
right to submit it under the project's license ([MIT](LICENSE)). Every commit needs a
`Signed-off-by` line:

```bash
git commit -s -m "Add filter for open requests"
```

This certifies the [Developer Certificate of Origin](https://developercertificate.org/).

## Development setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m backlog_pilot.app      # http://127.0.0.1:8000
```

Before you open a pull request, run the linter and the tests:

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

## Pull requests

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Keep each pull request to one change, with tests for new behavior.
3. Describe what changed and why, and how you tested it.
4. Keep demo data fictional. Never commit real names, customer data, credentials, or `.env`
   files.

If your change adds or copies third-party code, styles, or assets, add its license to
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Reporting bugs and security issues

Report bugs as GitHub issues, with steps to reproduce. Report security vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows its [Code of Conduct](CODE_OF_CONDUCT.md).
