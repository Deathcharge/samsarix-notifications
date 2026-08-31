# Samsarix Notifications roadmap

This roadmap separates four gates: merge, release, publication, and flagship adoption. Passing one does not imply the next.

## Product boundary

Portfolio role: **reusable library or sdk**. Keep this as a small, independently versioned package. Samsarix Unified should consume it only through a public API adapter; private monorepo imports and copied implementations are out of scope.

Current disposition: the productized Samsarix foundation and durable SQLite outbox are merged on `main` (PRs #2 and #3). The consumer-contract increment adds bounded backlog/JSON processing and a reproducible order-to-webhook journey from installed artifacts. Release, publication, and downstream adoption remain separate decisions.

## Stabilize the productized default

- Keep the default branch buildable from a clean checkout and preserve exact-head CI evidence.
- Keep Samsarix LLC branding, package identity, license metadata, and compatibility aliases internally consistent.
- Preserve the pre-productization default under a rollback ref before merging; do not delete legacy history.
- MPL-2.0, Samsarix LLC attribution, repository identity, and protected publishing are configured.
- Review priority: register the pending publisher on PyPI before creating a release.
- Review priority: run bounded live smokes.
- Review priority: integrate one real application using transactional enqueue and an idempotency policy.

## Release candidate

- Build and install the wheel in a clean environment.
- Prove the repository-owned reference consumer and versioned event fixture from an installed wheel; then obtain an independently owned application pilot before claiming adoption.
- Publish only after package-name ownership, licensing, provenance, and rollback are recorded.

Competitive direction:

- Differentiate from broad provider catalogs with a typed, secure, local-first reliability layer that needs no hosted control plane.
- The SQLite outbox supplies crash recovery, delayed delivery, cross-process idempotency, worker leases, bounded redelivery, provider receipts, and dead-letter recovery for single-host deployments.
- Next evaluate recipient-aware throttling/digests, delivery lifecycle callbacks or OpenTelemetry hooks, timestamped webhook signatures, and a small number of high-demand provider adapters. Add each only with a validated consumer journey.
- No distributed rate limit, preferences/consent store, digest engine, receipt polling, or provider-specific API adapter exists yet.
- DNS rebinding and ambiguous acknowledgement/retry duplication cannot be eliminated at this layer.
- No owner-authorized live SMTP or production webhook smoke, published package, release tag, or downstream consumer evidence exists.
- Direct in-memory idempotency does not compare payload fingerprints; the durable outbox does and rejects semantic conflicts.
- Next highest-value release gate is owner-coordinated validation: register the PyPI publisher, select one consumer pilot and its retention/replay policy, and authorize bounded live-provider smokes. More adapters or a hosted UI are not prerequisites for evaluating this library.

## Samsarix adoption

- Define a public API, event, schema, artifact, or deployment contract before connecting to Samsarix Unified.
- Add a consumer-owned contract fixture covering authentication, privacy, limits, errors, and version compatibility.
- Make one implementation canonical; remove or freeze duplicate behavior only after parity and rollback are proven.
- Record an owner, support level, compatibility window, and measurable adoption signal.

## Completion evidence

A milestone is complete only when its exact commit, commands and results, artifact digest, consumer or deployment, and rollback path are recorded in a pull request or release record. README claims must not exceed that evidence.
