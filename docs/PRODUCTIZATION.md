# Productization record

Last updated: 2026-08-31

## Repository assessment

The repository began as an async Python notification engine with `NotificationService`, `EmailService`, `WebhookRouter`, and `AlertSystem`. The April 2026 implementation advertised email, webhooks, Discord, Slack, SMS, push, retry logic, rate limiting, delivery tracking, templates, signatures, and idempotency, but every transport returned `True` without I/O. There were no tests, examples, CI workflows, release tags, or published PyPI package. A later ecosystem-wide documentation rewrite replaced useful package documentation with links to files and automation that did not exist.

The repository is independently useful as an embedded Python library. It is not evidence for a hosted service, frontend, database, AI feature, or private platform dependency. Before its first release, the owner moved the product and company identity from Helix to Samsarix; the distribution and import namespace were renamed at the same time to avoid shipping a permanent compatibility alias.

## Chosen product

**Product:** Samsarix Notifications, a local-first async Python notification-delivery library from Samsarix LLC with built-in SMTP email and JSON webhooks, an explicit result/error contract, bounded retries and concurrency, optional transactional SQLite delivery, process-local direct-send idempotency and history, and a small custom-transport protocol.

**Target user:** a Python application developer who needs one dependable boundary for transactional email, operational webhooks, or an application-specific transport without operating another notification platform.

**Primary journey:** commit an application change and a validated `NotificationPayload` together, deliver it through an `OutboxWorker` after commit, recover across process restarts, and inspect durable acceptance or a dead letter. Direct `NotificationService` delivery remains available without a database.

**Independent reason to exist:** unlike Apprise's broad provider catalog or hosted systems such as Courier and Novu, this package is intentionally small, open source, locally operated, typed, and focused on safe transport primitives that embed into an existing Python process. It has no private Samsarix runtime dependency.

**Deliberate non-goals:** hosted workflow design, subscriber profiles/preferences, distributed brokers, provider dashboards, SMS/push provider implementations, analytics, billing, AI content generation, authentication, a managed database, and a frontend. SQLite is an opt-in local persistence adapter, not a hosted service.

## Market evidence and consumer validation

Bounded primary-source research, refreshed 2026-08-31:

- [Apprise](https://github.com/caronc/apprise) offers a broad notification-provider catalog. Rebuilding that catalog is not this repository's differentiator.
- [Novu workflows](https://docs.novu.co/platform/workflow) organize multi-step notification orchestration. A workflow UI/control plane would materially expand this library's scope.
- [AWS transactional-outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) describes the database/message dual-write failure and the need for idempotent consumers. This supports the technical use case, not a claim of customer demand.

The resulting product decision is an embedded reliability boundary for single-host Python applications. `examples/durable_outbox.py` is a repository-owned reference consumer, using real SQLite and loopback HTTP. Its versioned `order_confirmed_v1.json` fixture locks the event shape. The journey proves transaction rollback, committed enqueue, HTTP 503 rescheduling, a fresh worker process accepting HTTP 202, persisted receipts, and duplicate enqueue after restart. CI repeats it against a non-editable wheel outside the checkout. This is reproducible technical validation, **not** evidence of external adoption, production throughput, exactly-once processing, or product-market fit.

## Product and architecture decisions

- `pyproject.toml` is the only package/dependency source. A library uses compatible dependency bounds rather than an application lockfile.
- Python 3.10 is the minimum because 3.9 reached end of life in October 2025; CI covers 3.10 through 3.14.
- HTTPX is the only runtime dependency. SMTP uses `smtplib` in `asyncio.to_thread`.
- Delivery success means the configured transport accepted the message; it does not claim recipient viewing or final provider delivery.
- Retries occur only for classified transient failures, are exponentially delayed, and are capped. Batch size and transport concurrency are independently bounded; no background retry worker is created.
- Direct idempotency and result history are bounded and process-local. `SQLiteOutbox` supports same-database transactions, fingerprinted cross-process deduplication, leases fenced by random claim tokens, scheduling, bounded redelivery, corruption quarantine, and explicit dead-letter recovery.
- Direct idempotency compares typed delivery-intent fingerprints and snapshots accepted keyed payloads, including nested metadata and email attachments. Cached results retain fingerprints rather than message content. ID/timestamp changes and mapping order do not change identity; sequence/scalar types and retry budgets do. Unsupported custom metadata is rejected explicitly for keyed sends, without restricting unkeyed custom-transport requests.
- `max_messages` defaults to 10,000 retained rows across all outbox states. A conditional insert atomically rejects growth at capacity, including on caller-owned autocommit connections. All writers must use the same configured limit; raw SQL and differently configured applications are outside that guarantee. Delivered-row retention is operator-owned.
- Outbox/webhook JSON encoding validates depth (32), visited nodes including keys (10,000), integer size (4,096 bits), and text budgets before encoding, then enforces exact encoded-byte limits. JSON keys must be strings; non-finite floats and unsupported objects are rejected.
- Webhooks require an exact operator host allowlist and default to public HTTPS on port 443, with no redirects, no ambient proxy trust, and DNS/IP checks before every send. Network egress rules remain recommended.
- Templates use `string.Template`; expression evaluation and recursive templating are out of scope.
- Legacy channel enum names remain extensibility keys, but only email and webhooks are built in.
- The unrelated `docs/index.html` portal was removed rather than maintained as misleading product UI.

## Assumptions

- The host application performs business authorization and consent/preference checks before calling the library.
- Operators provide valid SMTP credentials and external endpoints; tests use fakes or localhost only.
- Provider acknowledgement is the strongest portable synchronous delivery signal.
- Samsarix LLC has authority to rebrand and license the repository; publication credentials remain owner-controlled.

## Baseline command results

Baseline revision: `1a4acb0abac0e66ab821cfae0bd1978e2b05ab05` on clean `main`, matching `origin/main`.

| Command | Baseline result |
| --- | --- |
| `python --version` | Passed: Python 3.11.9. |
| `python -c "import helix_notifications; print(helix_notifications.__version__)"` | Passed and printed `1.0.0`. |
| `python -m compileall -q helix_notifications` | Passed. |
| `python -m pytest -q` | Failed: no tests were collected. |
| `python -m ruff check .` | Failed: one unused import. |
| `python -m mypy helix_notifications` | Failed: seven type errors. |
| isolated `python -m pip install -e .` | Passed, but installed unused Pydantic plus HTTPX. |
| isolated `python -m pip install -r requirements.txt` | Failed: `anthropic==0.7.10` had no matching Python 3.11 distribution; the file also contained unrelated web, AI, data, database, Discord, Redis, and Celery dependencies. |
| `python -m build` | Passed with duplicate-metadata and deprecated/incorrect Apache-license warnings; the wheel included both conflicting license files. |

## Prioritized findings

### P0

- All advertised transports falsely returned success without performing delivery.
- The documented requirements installation failed and installed unrelated platform dependencies.
- The README advertised nonexistent examples, documents, CI, coverage, and a production-ready state.
- Package metadata declared Apache-2.0 while `LICENSE` contained Business Source License 1.1 and named a different “Licensed Work”; owner confirmation was a release gate until the Samsarix/MPL-2.0 transition resolved it.

### P1

- No tests or CI protected the public API or package artifact.
- Inputs, timeouts, retry classification, concurrency, caches, and alert growth were unbounded or absent.
- Webhook destination validation, redirect policy, signatures, and network behavior were missing.
- SMTP TLS/authentication, MIME construction, template behavior, attachments, and provider errors were missing.
- Batch delivery overwrote duplicate recipients, failures did not actually retry, and delivery tracking was never populated.
- Packaging metadata was duplicated across `setup.py`, `pyproject.toml`, and copied requirements files.
- The static site described an unrelated rituals repository and live infrastructure.

### P1 follow-up closed in the consumer increment

- The merged outbox lacked a total backlog cap; queue capacity now provides explicit backpressure and preserves application rollback.
- Payload size checks ran after JSON serialization; traversal and allocation are now bounded before serialization for both webhook paths and the outbox.
- The durable example used a stand-in transport and wheel verification only imported a module; the real reference consumer now runs from installed artifacts across fresh worker processes.
- The living product record was stale after PR #3; this revision reconciles persistence, costs, release evidence, and remaining gates.

### Direct-delivery lifecycle follow-up

On baseline `98262db`, all 16 existing dispatcher tests passed, but two new regression tests failed: cancelling the sole idempotent waiter left its completed task in `_inflight`, and `aclose()` closed a transport before its accepted send completed. These were locally actionable lifecycle defects, not external release gates.

The lifecycle increment moves completion/cache bookkeeping to delivery-owned callbacks, tracks and caps all accepted running/queued operations, and returns a retryable zero-attempt `service_busy` result at capacity. Shutdown refuses new admissions, drains accepted tasks, then attempts cleanup for every currently registered unique transport. Concurrent or cancelled shutdown waiters share one cleanup task. Drain/cleanup grace expiration is explicit and does not imply provider non-delivery. Eager asyncio task factories cannot invoke custom transports before admission records are published.

Validation includes cancelled owners and duplicates, released capacity after queued/pre-start cancellation, failed detached retries, shutdown timeouts, concurrent close, closer failures, and actual HTTP acceptance after request cancellation. The original two reproductions now pass. This follows [Python's documented task ownership and cancellation semantics](https://docs.python.org/3/library/asyncio-task.html), not an assumption that cancellation forcibly stops threads or custom non-cooperative code.

### P2

- Add opt-in adapters for specific providers only after real user demand.
- Evaluate recipient-aware throttling/digests only after a consumer demonstrates the need.
- Add provider-specific receipt polling only where an API can support it consistently.
- Consider redacting or hashing retained recipient identifiers through an opt-in result-store adapter.

### Direct-idempotency follow-up

On baseline `077a94e`, two regressions reproduced incorrect direct-delivery behavior: a key reused for another recipient received the first recipient's cached success, and mutation after admission changed the accepted payload/result. The outbox already rejected intent conflicts, but the direct dispatcher only indexed keys. Parameter consistency is an established retry-contract safeguard, as described in [Stripe's idempotency documentation](https://docs.stripe.com/api/idempotent_requests); this library retains its own success-only, bounded process-local cache semantics rather than adopting Stripe's retention/failure policy.

The fix returns non-retryable, zero-attempt `idempotency_conflict` before waiting or transport I/O, preserving the original operation and cache entry. It hashes type-tagged, length-framed values and snapshots mutable containers at admission. Work is bounded to 32 levels, 10,000 visited values/keys, 4,096-bit integers, and 64 MiB of identity data. Native data and email attachments are supported; arbitrary object hooks, repr-based identity, and pickle are not used. No durable format, database schema, or runtime dependency changes.

## Implementation checklist

- [x] Define the narrow product and non-goals.
- [x] Replace false-success dispatch with typed transport results and errors.
- [x] Implement SMTP email delivery and safe templates.
- [x] Implement secure JSON webhook delivery and named routes.
- [x] Add bounded retry, timeout, concurrency, history, and idempotency behavior.
- [x] Bound and validate process-local alert state.
- [x] Make modern package metadata authoritative and remove unrelated requirements.
- [x] Complete unit and end-to-end tests.
- [x] Add CI and verify supported Python/package shape.
- [x] Complete README, security policy, example, and release documentation.
- [x] Run final clean-environment and adversarial verification.
- [x] Add durable SQLite delivery and bounded queue backpressure.
- [x] Prove an order-confirmation reference consumer across real HTTP failure and worker restart.
- [x] Add a versioned event fixture and exercise the journey from installed wheels in CI.
- [x] Reproduce and fix direct-delivery task retention, bounded admission, and shutdown ordering.
- [x] Add unpublished release-candidate validation with an artifact round trip and wheel-only execution of the sdist tests/examples.
- [x] Reject direct idempotency intent conflicts and isolate admitted keyed payloads from later caller mutation.
- [x] Add continuous representative Linux/Windows/macOS wheel tests and an explicit HTTPX lower-bound job.
- [ ] Obtain an external consumer pilot and bounded live-provider acceptance evidence (owner-coordinated).

## Release acceptance criteria

- Source and wheel installation succeed on a supported Python version.
- The local webhook example completes without credentials or external network access.
- The durable order example completes with two separate worker processes, a real loopback receiver, and the versioned event fixture.
- SMTP and webhook transports have deterministic interface-level tests, including failure and retry cases.
- Ruff formatting/lint, strict mypy, tests with at least 90% branch coverage, build, Twine metadata check, and wheel smoke import all pass.
- CI runs the meaningful checks on supported Python versions.
- Representative Windows/macOS jobs and the declared HTTPX lower bound pass the full installed-wheel suite and both consumer examples.
- The release workflow validates downloaded distributions without a checkout; pull-request and manual candidate runs skip the protected publish job.
- No built-in transport reports success without provider acceptance.
- Webhook private-address, redirect, payload-limit, timeout, and signature behavior is covered.
- README commands and public API examples match the built artifact.
- No locally actionable P0 remains.
- Package metadata, source notices, and built artifacts consistently identify MPL-2.0 and Samsarix LLC.

## Foundation verification (historical, PR #2)

| Gate | Result |
| --- | --- |
| Ruff format and lint | Passed. |
| Strict mypy over the library and tests | Passed: 15 files. |
| Unit and localhost integration tests | 69 passed on Python 3.13.14. |
| Branch-aware coverage | 96.95%, above the 90% gate. |
| Local webhook example | Passed without credentials or external network access. |
| Wheel and source build | Passed from `pyproject.toml`. |
| Twine metadata validation | Passed for both artifacts. |
| Non-editable wheel smoke import | Passed outside the repository. |
| Workflow validation | Both GitHub Actions workflows passed `actionlint` 1.7.12. |
| Hosted CI | Passed on Python 3.10, 3.11, 3.12, 3.13, and 3.14 in pull request #2. |
| Runtime dependency audit | No known vulnerabilities reported; the local package is not yet on PyPI. |

## Consumer-increment verification

Environment: fresh `.venv`, Windows, Python 3.14.7. Commands are run through `.venv/Scripts/python.exe` on Windows (or `.venv/bin/python` on Unix).

- `python -m pip install -e ".[dev]"`: passed in the new environment.
- `python -m ruff format --check .`, `python -m ruff check .`, `python -m mypy`: passed; strict mypy checked 20 files. The durable example also passed a separate strict mypy check.
- `python -m pytest --cov=samsarix_notifications --cov-report=term-missing`: 125 passed, 94.31% branch-aware coverage, including queue-race, rollback, JSON-bound, corruption, SMTP/webhook, and subprocess-consumer regressions.
- `python -I examples/durable_outbox.py`: passed; two worker processes, two HTTP attempts, one accepted event, durable delivered status, duplicate not created.
- `python -m build --outdir dist/consumer-final-20260831` and `python -m twine check dist/consumer-final-20260831/*`: passed for wheel/sdist; examples and fixture ship in the sdist.
- Non-editable wheel verification passed both examples outside the checkout with `-I` isolated imports. The durable example extracted from the sdist also passed against that wheel. Exact artifact digests and hosted-CI results belong to the PR, not a floating readiness claim.

## Completed work

### Release-readiness audit and local-data hygiene (2026-08-31)

Audit baseline: clean `main` at `8a1fde7fd51a789411ececf05d69bacbd9bd7326`, synchronized with origin. The preceding turn made concrete progress through merged provider-retry handling; this audit checks the broader objective rather than treating another green increment as proof of production adoption.

| Requirement group | Current authoritative evidence | Disposition |
| --- | --- | --- |
| Independent product, target user, and primary journey | README, public API, single HTTPX runtime dependency, transactional order fixture/consumer | Implemented as an embedded Python library, not a hosted platform. |
| Samsarix identity, attribution, and package license | Manifest, NOTICE, CITATION.cff, SPDX notices, installed-distribution metadata tests | Samsarix LLC, supplied contact addresses, and MPL-2.0 are in place. |
| Real delivery and ordinary failure recovery | Local HTTP consumer, SMTP interface tests, typed errors, retry/dead-letter/cancellation tests | Locally verified; real SMTP/provider acceptance remains unverified. |
| Durability, limits, and lifecycle | Transaction/rollback, restart, lease-fencing, backlog, idempotency, retry-hint, and shutdown tests | Covered by the 302-test suite; not exactly-once delivery or a multi-host broker. |
| Source/artifact and representative compatibility | [Merged-main CI 33391978081](https://github.com/Deathcharge/samsarix-notifications/actions/runs/33391978081), [artifact validation 33391437320](https://github.com/Deathcharge/samsarix-notifications/actions/runs/33391437320), PR #10 hashes/results | Eight CI jobs and no-checkout wheel/sdist verification passed; older Python versions intentionally skip one unavailable eager-task-factory case. |
| Release controls | Current workflow plus GitHub environment API: owner review required, only `v*` tags allowed, publishing gated on release events | Configured, but registry exchange/publication has not been exercised. |
| Privacy, cost, and operating guidance | README and outbox guide document plaintext storage, retention/replay, bounded attempts, provider ambiguity, and deployment controls | Documented limits, not an independent audit or production certification. |
| External distribution and use-case validation | GitHub API returned zero releases; public PyPI project lookup returned 404; no independently owned pilot target or provider acceptance record supplied | Incomplete external gates. A public 404 cannot verify private pending-publisher configuration. |

The audit found an actionable repository-hygiene gap: `.env` variants and normal outbox database names were not ignored. `git check-ignore --no-index` reproduced this without reading or creating sensitive files. Ignore rules now cover local environment files, `.db`/`.sqlite`/`.sqlite3` databases, and their journal/WAL/SHM sidecars; `.env.example` remains shareable for placeholders. Validation passed for 18 ignored paths and six allowed source/template paths, and `git ls-files` found no matching tracked environment/database files. Ignore rules do not remove existing history, encrypt data, protect backups, or prevent force-staging. Contributor and privacy guidance now make those limits explicit.

The unresolved next step is an owner-selected application and test transport, with the acceptance/retention/replay criteria below. Additional adapters, a UI, or synthetic examples cannot supply independent adoption evidence. The full goal remains unproven until the external gates are resolved; the supported disposition is a release candidate.

### Provider-directed retries (2026-08-31)

On baseline `3fd86ef`, two new regression tests failed: after HTTP 429 with `Retry-After: 60`, direct delivery made four immediate attempts and a zero-backoff worker claimed the same row three times in one run. This exhausted local budgets while ignoring a provider minimum. [RFC 9110 section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3) defines integer-second/date hints, and [Slack documents HTTP 429 plus Retry-After](https://docs.slack.dev/apis/web-api/rate-limits/) for rate-limited HTTP APIs, including incoming webhooks. These sources establish a concrete interoperability need, not evidence of Samsarix customer demand or a new built-in Slack adapter.

Webhook parsing now preserves supported hints on `DeliveryError` and final `DeliveryResult`, using an additive optional `retry_after_seconds` field. Direct retries wait for the greater of local backoff and provider minimum, or return a retryable failure when the wait exceeds the inline delay budget. The outbox commits the later `available_at` and releases its claim without sleeping. Parsing is bounded to 128 characters and one day: malformed short values fall back to local backoff; unsupported long waits stop automatic retries for operator review instead of being clamped early. No schema/dependency changes; existing attempt budgets remain in force. Per-message scheduling deliberately does not infer provider-wide quota groups.

Local Windows/Python 3.14.7 checks passed Ruff formatting/lint, strict mypy (27 files), and **302 tests at 95.91% branch-aware coverage**, including 51 focused retry-hint tests. Review also reproduced Python email parsing's fixed-century interpretation of an RFC 850 date as a past date; the parser now applies HTTP's rolling 50-year rule, tested across the boundary and century rollover. The reference consumer receives HTTP 503 with a one-second hint, verifies its committed deadline, exits its first worker, and obtains HTTP 202 from a second worker after that deadline. Its versioned event shape is unchanged. Exact-head artifact/compatibility evidence belongs in the corresponding PR after execution. Publication, authorized provider smokes, and an independent pilot remain unexecuted external gates.

### Configuration validation (2026-08-31)

On baseline `e3b545b`, three new regression tests failed: `RetryPolicy(max_retries=0.5)` was accepted until later retry iteration, a dispatcher mapping silently truncated `concurrency_limit=1.5`, and `list_messages(limit=1.5)` reached SQLite and raised a raw database error. These are closed P1 input-contract defects, not provider failures.

Shared private predicates now require native integers for counts/ports/byte limits, finite native integers or floats for durations, and native booleans for SMTP TLS flags, webhook private-address configuration, and outbox initialization. They reject unsupported objects without invoking numeric conversion/comparison hooks. Public one-based retry numbers are bounded before exponentiation. Mapping and keyword inputs follow the same rules; existing valid bounds, fractional durations, zero-budget options, error classes/codes, database schema, and dependencies are unchanged. Numeric-string coercion in pre-release dispatcher mappings is intentionally removed and documented in the README.

Local Windows/Python 3.14.7 verification: **251 tests passed at 95.67% branch-aware coverage**, including 48 configuration tests covering wrong types, non-finite numbers, lower/upper bounds, stable typed errors, and rejection before database creation/access. Ruff formatting/lint and strict mypy (25 files) passed. The `42ba733` wheel also passed all 251 sdist tests, both examples, and `pip check` in a fresh environment outside the checkout with isolated imports and HTTPX 0.27.0. Exact-head artifact and hosted compatibility results are recorded in the corresponding PR after execution; these checks do not exercise live providers or establish a completed independent security audit.

### Compatibility verification (2026-08-31)

The audit found that OS-independent package metadata and the HTTPX `>=0.27` lower bound exceeded continuous verification: hosted CI covered Linux with current resolved dependencies, while Windows had only local evidence. CI now adds Windows/macOS Python 3.14 jobs and a Linux/Python 3.10 HTTPX 0.27.0 job without replacing the five existing Linux version checks. Every matrix job runs `pip check`, the source checks/build, and the full suite plus both examples against a non-editable wheel outside the checkout. The portable wheel step uses Python subprocess argument lists and platform-specific virtual-environment executable paths; it does not assume Unix paths on Windows.

Local execution of the exact portable wheel step on Windows/Python 3.14.7 with HTTPX 0.27.0 passed 203 tests, both examples, and dependency consistency. Ruff formatting/lint, strict mypy (23 files), and actionlint 1.7.12 passed. Hosted OS/version/dependency results and artifact identity are recorded in the corresponding PR. This tests representative platforms and the direct HTTPX floor, not every version combination or every transitive dependency minimum. Owner publication/provider/pilot actions are specified below and remain unexecuted.

### Direct-idempotency verification (2026-08-31)

On Windows/Python 3.14.7, the complete suite passed **203 tests at 95.26% branch-aware coverage**; Ruff formatting/lint, strict mypy (23 files), both isolated localhost examples, and actionlint 1.7.12 also passed. The two baseline failures now pass. New tests cover every compared field in both cached and active states, ID/timestamp regeneration, mapping order, type/framing distinctions, nested snapshot isolation, binary/object attachments through the SMTP interface, real HTTP conflicts after caller cancellation, cache eviction, cleanup, unsupported values, and resource bounds. Exact-head distribution and hosted-CI evidence belongs to the corresponding PR. Durable schema/fingerprint semantics are unchanged.

### Release-candidate validation (2026-08-31)

CI and release workflows now pin verified Node 24 action releases: checkout 7.0.1, setup-python 7.0.0, upload-artifact 7.0.1, and download-artifact 8.0.1. The previous upload/download comments did not match their pinned runtime; all four replacements were checked against upstream action manifests, not only version labels.

The release workflow runs on pull requests and manual dispatch as an unpublished candidate check. It uploads the built wheel/sdist and downloads them into a fresh job without a checkout, fails artifact-digest mismatches, records package SHA-256 hashes, and runs the sdist's full test suite and both examples against the isolated installed wheel. Candidate artifacts expire after seven days. Publication remains a separate, release-event-only job behind artifact validation and owner approval; PR/manual runs do not request publication approval or OIDC permissions. The workflow filename remains `release.yml` for the pending PyPI Trusted Publisher configuration.

Local verification passed actionlint 1.7.12, Ruff formatting/lint, strict mypy (21 files), 154 tests at 94.85% branch-aware coverage, isolated wheel/sdist builds, and Twine metadata validation. All 154 sdist tests and both examples also passed against the non-editable wheel outside the checkout with isolated imports. Hosted event/round-trip evidence is recorded in the corresponding PR; none of these checks publish a package or establish live-provider acceptance.

### Lifecycle verification (2026-08-31)

On Windows/Python 3.14.7, `python -m pytest --cov=samsarix_notifications --cov-report=term-missing` passed **154 tests at 94.85% branch-aware coverage**. `python -m ruff format --check .`, `python -m ruff check .`, and strict `python -m mypy` passed (21 files). Both `python -I examples/local_webhook.py` and `python -I examples/durable_outbox.py` passed after the lifecycle changes. The lifecycle suite includes one eager-task-factory test that is intentionally skipped on Python 3.10/3.11, where that facility does not exist. Exact-head distribution hashes, wheel-only verification, and hosted Python-version results are recorded in the lifecycle PR.

The repository now has real SMTP and webhook transports; explicit results and stable error codes; bounded retry, timeout, concurrency, batch, history, idempotency, attachment, JSON, outbox backlog, and alert behavior; durable SQLite recovery; and truthful custom-channel extensibility. It has deterministic tests, real localhost reference consumers, supported-version CI, modern single-source packaging, and release documentation. Ordinary targeted code review and regression tests cover this increment; the interrupted app-backed scan is not claimed as a completed security audit. No known locally actionable P0 remains, but this is not a certification of security or production deployment.

**Disposition:** release candidate, subject to the registry gate below. The package is independently useful now; no hosted Samsarix service or adjacent repository is required.

## Deferred and externally blocked work

- **Owner/publication:** register the `samsarix-notifications` pending publisher on PyPI for repository `Deathcharge/samsarix-notifications`, workflow `release.yml`, and environment `pypi`. The protected GitHub environment and its required owner approval are configured, and hosted CI passed. No external package was published.
- **Credentials/providers:** live SMTP and production webhook smoke tests require owner-supplied endpoints and would create external side effects. Local fakes and a localhost end-to-end receiver cover the interfaces without cost.
- **Adoption:** select a real application owner, consent/authorization policy, retention window, and acceptable delivery latency before a production pilot. The reference consumer cannot establish external demand or independently owned compatibility.
- **Portfolio:** no changes to another Samsarix repository are required.

### Owner release and pilot handoff

These are the remaining external gates, not permission to publish or contact providers automatically. No credentials belong in commits, artifacts, public issues, or chat transcripts.

1. **PyPI identity:** sign in as the intended package owner and open **Account settings → Publishing → Add a new pending publisher → GitHub**. Enter project `samsarix-notifications`, owner `Deathcharge`, repository `samsarix-notifications`, workflow filename `release.yml` (not its path), and environment `pypi`. Verify that the pending-publisher entry displays those exact values. Pending publishers do not reserve a package name; recheck ownership before first publication. See [PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
2. **Candidate approval:** run `gh workflow run release.yml --ref main`, open the resulting Actions run, and retain the commit SHA, wheel/sdist hashes, and `python-package-distributions` artifact before its seven-day expiry. Confirm build and downloaded-artifact jobs passed and publishing was skipped. Confirm the `pypi` environment still requires owner review. A green manual run does not test the registry's OIDC exchange.
3. **Explicit publication decision:** only after identity, candidate, and owner approval, publish the GitHub Release for a tested commit using the version-matched tag (`v0.1.0` for the current package). Approve that run's `pypi` environment only after its build/artifact validation. Verify the public package/version, file hashes, and provenance against that release run, then test registry installation in a fresh environment. Do not rewrite an already published version or silently claim the pending publisher was exercised by CI; no registry publication has been attempted here.
4. **Bounded provider acceptance:** supply an explicitly approved test recipient and SMTP configuration (`SMTP_HOST`, integer `SMTP_PORT`, `SMTP_USERNAME`, secret `SMTP_PASSWORD`, and authorized `SMTP_FROM`) and/or an owned HTTPS webhook URL with an exact allowed hostname. Agree to one synthetic notification per selected transport, no automatic retries (`RetryPolicy(max_retries=0)` and payload `max_retries=0`), a unique operation key, and a repeat of that same key to verify cached deduplication without another provider call. Record a sanitized result and receiver/provider receipt. After a timeout, inspect provider receipts before any resend; acceptance may be ambiguous. SMTP acceptance is not an inbox-delivery claim, nor is HTTP 2xx a downstream-processing claim.
5. **Independent pilot:** name an application owner and test environment; record the tested wheel hash, public API/event fixture, expected delivery latency and volume, consent/authorization policy, tenant-scoped key strategy, replay/retention window, outbox row/disk budget, and rollback owner. Reproduce commit/rollback, provider failure/recovery, worker restart, duplicate suppression, dead-letter inspection/requeue, and shutdown. Record actual observations against those agreed criteria. Stop the worker on an unexpected recipient or duplication and preserve the database for owner-controlled inspection. The repository-owned order example proves its technical contract, not this independently owned acceptance.

## Known risks

- DNS rebinding remains possible between application-level resolution and HTTPX connection. Exact host allowlists are required by default and network egress policy is the strongest deployment control.
- SMTP acceptance is not proof of inbox delivery; webhook `2xx` is not proof of downstream processing.
- Process-local delivery history and idempotency disappear on restart and do not coordinate multiple workers.
- Durable delivery coordinates workers on one local SQLite database, not across hosts/regions. It is not a globally ordered event log. Backups, private filesystem permissions, disk quotas, retention, and plaintext-content privacy remain operator responsibilities.
- The row cap is not a filesystem-byte quota. SQLite free pages, journals, application tables, and backups use additional space. Purging delivered rows also removes their durable deduplication history; never purge earlier than the business replay window.
- Notification content and recipient identifiers remain in application memory while being sent; provider privacy terms remain the operator's responsibility.
- Retries can duplicate an external effect when a provider accepts work but the acknowledgement is lost. Callers should supply idempotency keys and providers should deduplicate when possible.
- Shutdown grace periods require cooperative transports and a running event loop. Cancelling an SMTP waiter cannot stop a thread already communicating with the provider. Direct idempotency rejects conflicting intent only while a key is active or cached; eviction/failure/restart ends that protection. Correct application/tenant key scoping remains a caller responsibility.

## Distribution and sustainability

The simplest distribution path is a pure-Python wheel and source distribution built from `pyproject.toml`, published to PyPI through the protected GitHub Release workflow after the owner configures Trusted Publishing. The package itself needs no hosted infrastructure. MPL-2.0 keeps distributed modifications to covered files open while allowing broad embedding; sustainability can come from paid support, integration work, or a separately operated hosted offering. No subscription economics or validated demand are assumed.

Runtime cost is provider-driven. Direct attempts are bounded by `N * (1 + R)` (default `R=3`, concurrency 10). Durable attempts are bounded by queued notifications times `max_delivery_attempts` (default 5), with no nested dispatcher retries; explicit operator requeue starts a new budget. Tests and examples make no paid provider calls.
