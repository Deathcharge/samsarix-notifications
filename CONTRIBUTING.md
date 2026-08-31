# Contributing to Samsarix Notifications

Do not commit provider credentials, local environment files, or notification databases. The repository ignores `.env`/`.env.*` (except a placeholder-only `.env.example`) and `.db`/`.sqlite`/`.sqlite3` files plus SQLite sidecars. These rules are a staging safeguard, not access control, encryption, or removal from Git history. Review `git diff --cached` before committing; use synthetic JSON fixtures for tests rather than real notification data.

Thanks for helping improve the package. Contributions should preserve its narrow purpose: a dependable embedded Python delivery boundary for email, webhooks, and custom transports, with optional local SQLite persistence. Hosted infrastructure, managed/distributed databases, user management, and unrelated platform code are out of scope unless the product definition changes explicitly.

## Setup

```bash
git clone https://github.com/Deathcharge/samsarix-notifications.git
cd samsarix-notifications
python -m venv .venv
```

Activate `.venv`, then install the package and development tools:

```bash
python -m pip install -e ".[dev]"
```

## Required checks

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest --cov=samsarix_notifications --cov-report=term-missing
python -m build
python -m twine check dist/*
```

Add focused tests for behavior changes. Network tests must use a local server or deterministic HTTPX transport; CI must never require live provider credentials or spend money. Public functions and classes require type annotations and concise docstrings.

The CI matrix covers Linux on Python 3.10–3.14, Windows/macOS on Python 3.14, and HTTPX 0.27.0 on Linux/Python 3.10. Each job repeats the full tests and examples from an isolated installed wheel. Keep virtual-environment executable paths portable (`Scripts/python.exe` on Windows, `bin/python` elsewhere), and update the lower-bound job when changing the supported runtime dependency range. This job tests the direct HTTPX minimum, not every transitive dependency's minimum.

## Security-sensitive changes

Treat webhook destination validation, redirect behavior, SMTP TLS/authentication, retry classification, idempotency, payload limits, and logging as security-sensitive. Include a negative test for the bypass or failure state being changed. Never add real credentials, provider tokens, private message content, or production endpoints to fixtures.

## Pull requests

Keep changes small enough to review, explain the user-visible outcome, and include the exact commands you ran. Do not claim a provider integration works without an interface-level test. Use conventional commit prefixes such as `feat:`, `fix:`, `docs:`, `test:`, and `chore:` when practical.

The release workflow runs a non-publishing artifact round trip on every pull request. It installs the downloaded wheel in a fresh job without a checkout and runs tests and examples extracted from the source distribution. A manual candidate check is available with `gh workflow run release.yml --ref main`; this also skips publication and does not request environment approval or OIDC permissions.

The default branch is not a publication trigger. Only publishing a version-matched GitHub Release can invoke the protected PyPI publish job after artifact validation; the `pypi` environment requires owner approval. License changes, credentials, and production deployments remain owner-controlled.
