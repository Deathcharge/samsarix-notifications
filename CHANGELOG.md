# Changelog

All notable changes will be documented here. The project follows semantic versioning after its first published release.

## 0.1.0 - Unreleased

- Rebrand the project and Python namespace as Samsarix Notifications under Samsarix LLC.
- Adopt Mozilla Public License 2.0 with explicit copyright, support, and licensing contacts.
- Add protected, attestable PyPI Trusted Publishing automation for GitHub Releases.
- Replace simulated-success transports with real SMTP and HTTP webhook delivery.
- Add explicit delivery results, typed errors, bounded retries, timeouts, concurrency, history, and process-local idempotency.
- Add secure webhook destination policy, no-redirect behavior, payload limits, and optional HMAC signing.
- Add safe email templates, address validation, TLS configuration, and bounded in-memory attachments.
- Add a complete deterministic test suite, local end-to-end example, CI, and package verification.
- Replace copied platform dependencies and duplicate packaging metadata with one `pyproject.toml` source of truth.
- Correct documentation and package maturity claims; record the owner-controlled license gate.
