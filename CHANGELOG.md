# Changelog

All notable changes will be documented here. The project follows semantic versioning after its first published release.

## 0.1.0 - Unreleased

- Bound retained outbox rows with atomic capacity backpressure and preserve application rollback at capacity.
- Validate JSON complexity and size before serialization for outbox and webhook payloads; quarantine deeply malformed stored JSON.
- Verify a versioned order-confirmation consumer over real loopback HTTP across worker-process restarts, including from installed wheels in CI.
- Include runnable examples, their event fixture, and operations documentation in source distributions.
- Gate release artifacts on lint, typing, tests, and installed-wheel consumer journeys before protected publishing.
- Rebrand the project and Python namespace as Samsarix Notifications under Samsarix LLC.
- Adopt Mozilla Public License 2.0 with explicit copyright, support, and licensing contacts.
- Add an optional durable SQLite outbox with transactional enqueue, scheduling, cross-process leases, retry rescheduling, dead-letter recovery, bounded retention, and a background worker.
- Expose final failure retryability so durable callers can distinguish rescheduling from permanent failure.
- Add a credential-free transactional order example and an operations guide for at-least-once delivery.
- Add protected, attestable PyPI Trusted Publishing automation for GitHub Releases.
- Replace simulated-success transports with real SMTP and HTTP webhook delivery.
- Add explicit delivery results, typed errors, bounded retries, timeouts, concurrency, history, and process-local idempotency.
- Add secure webhook destination policy, no-redirect behavior, payload limits, and optional HMAC signing.
- Add safe email templates, address validation, TLS configuration, and bounded in-memory attachments.
- Add a complete deterministic test suite, local end-to-end example, CI, and package verification.
- Replace copied platform dependencies and duplicate packaging metadata with one `pyproject.toml` source of truth.
- Correct documentation and package maturity claims; record the owner-controlled license gate.
