# Samsarix Notifications roadmap

This roadmap separates four gates: merge, release, publication, and flagship adoption. Passing one does not imply the next.

## Product boundary

Portfolio role: **reusable library or sdk**. Keep this as a small, independently versioned package. Samsarix Unified should consume it only through a public API adapter; private monorepo imports and copied implementations are out of scope.

Current disposition: Merge the productization branch after exact-head verification and rollback-ref creation; release and adoption remain separate decisions.

## Stabilize the productized default

- Keep the default branch buildable from a clean checkout and preserve exact-head CI evidence.
- Keep Samsarix LLC branding, package identity, license metadata, and compatibility aliases internally consistent.
- Preserve the pre-productization default under a rollback ref before merging; do not delete legacy history.
- Review priority: Authorize license.
- Review priority: review protected publisher.
- Review priority: run bounded live smokes.
- Review priority: integrate one app with durable enqueue and idempotency policy.

## Release candidate

- Build and install the wheel in a clean environment.
- Prove one real consumer and a versioned compatibility fixture.
- Publish only after package-name ownership, licensing, provenance, and rollback are recorded.

Current hardening backlog:

- Material BSL-to-MPL relicensing and deletion of proprietary terms require owner/legal approval.
- No durable queue, cross-process idempotency, distributed rate limit, preferences/consent store, receipt polling, or provider-specific adapter exists.
- DNS rebinding and ambiguous acknowledgement/retry duplication cannot be eliminated at this layer.
- No owner-authorized live SMTP or production webhook smoke, published package, release tag, or downstream consumer evidence exists.
- Using the same idempotency key for semantically different payloads is a caller error the cache does not detect.

## Samsarix adoption

- Define a public API, event, schema, artifact, or deployment contract before connecting to Samsarix Unified.
- Add a consumer-owned contract fixture covering authentication, privacy, limits, errors, and version compatibility.
- Make one implementation canonical; remove or freeze duplicate behavior only after parity and rollback are proven.
- Record an owner, support level, compatibility window, and measurable adoption signal.

## Completion evidence

A milestone is complete only when its exact commit, commands and results, artifact digest, consumer or deployment, and rollback path are recorded in a pull request or release record. README claims must not exceed that evidence.
