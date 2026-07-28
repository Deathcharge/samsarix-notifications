# Contributing to Helix Notifications

Thanks for helping improve the package. Contributions should preserve its narrow purpose: a dependable embedded Python delivery boundary for email, webhooks, and custom transports. Hosted infrastructure, databases, user management, and unrelated Helix platform code are out of scope unless the product definition changes explicitly.

## Setup

```bash
git clone https://github.com/Deathcharge/helix-notifications.git
cd helix-notifications
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
python -m pytest --cov=helix_notifications --cov-report=term-missing
python -m build
python -m twine check dist/*
```

Add focused tests for behavior changes. Network tests must use a local server or deterministic HTTPX transport; CI must never require live provider credentials or spend money. Public functions and classes require type annotations and concise docstrings.

## Security-sensitive changes

Treat webhook destination validation, redirect behavior, SMTP TLS/authentication, retry classification, idempotency, payload limits, and logging as security-sensitive. Include a negative test for the bypass or failure state being changed. Never add real credentials, provider tokens, private message content, or production endpoints to fixtures.

## Pull requests

Keep changes small enough to review, explain the user-visible outcome, and include the exact commands you ran. Do not claim a provider integration works without an interface-level test. Use conventional commit prefixes such as `feat:`, `fix:`, `docs:`, `test:`, and `chore:` when practical.

The default branch is not a publication trigger. Package publishing, licensing decisions, credentials, and production deployments remain owner-controlled.
