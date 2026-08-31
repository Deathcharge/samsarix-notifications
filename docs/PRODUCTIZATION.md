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

### P2

- Add opt-in adapters for specific providers only after real user demand.
- Evaluate recipient-aware throttling/digests only after a consumer demonstrates the need.
- Add provider-specific receipt polling only where an API can support it consistently.
- Consider redacting or hashing retained recipient identifiers through an opt-in result-store adapter.

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
- [ ] Obtain an external consumer pilot and bounded live-provider acceptance evidence (owner-coordinated).

## Release acceptance criteria

- Source and wheel installation succeed on a supported Python version.
- The local webhook example completes without credentials or external network access.
- The durable order example completes with two separate worker processes, a real loopback receiver, and the versioned event fixture.
- SMTP and webhook transports have deterministic interface-level tests, including failure and retry cases.
- Ruff formatting/lint, strict mypy, tests with at least 90% branch coverage, build, Twine metadata check, and wheel smoke import all pass.
- CI runs the meaningful checks on supported Python versions.
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

The repository now has real SMTP and webhook transports; explicit results and stable error codes; bounded retry, timeout, concurrency, batch, history, idempotency, attachment, JSON, outbox backlog, and alert behavior; durable SQLite recovery; and truthful custom-channel extensibility. It has deterministic tests, real localhost reference consumers, supported-version CI, modern single-source packaging, and release documentation. Ordinary targeted code review and regression tests cover this increment; the interrupted app-backed scan is not claimed as a completed security audit. No known locally actionable P0 remains, but this is not a certification of security or production deployment.

**Disposition:** release candidate, subject to the registry gate below. The package is independently useful now; no hosted Samsarix service or adjacent repository is required.

## Deferred and externally blocked work

- **Owner/publication:** register the `samsarix-notifications` pending publisher on PyPI for repository `Deathcharge/samsarix-notifications`, workflow `release.yml`, and environment `pypi`. The protected GitHub environment and its required owner approval are configured, and hosted CI passed. No external package was published.
- **Credentials/providers:** live SMTP and production webhook smoke tests require owner-supplied endpoints and would create external side effects. Local fakes and a localhost end-to-end receiver cover the interfaces without cost.
- **Adoption:** select a real application owner, consent/authorization policy, retention window, and acceptable delivery latency before a production pilot. The reference consumer cannot establish external demand or independently owned compatibility.
- **Portfolio:** no changes to another Samsarix repository are required.

## Known risks

- DNS rebinding remains possible between application-level resolution and HTTPX connection. Exact host allowlists are required by default and network egress policy is the strongest deployment control.
- SMTP acceptance is not proof of inbox delivery; webhook `2xx` is not proof of downstream processing.
- Process-local delivery history and idempotency disappear on restart and do not coordinate multiple workers.
- Durable delivery coordinates workers on one local SQLite database, not across hosts/regions. It is not a globally ordered event log. Backups, private filesystem permissions, disk quotas, retention, and plaintext-content privacy remain operator responsibilities.
- The row cap is not a filesystem-byte quota. SQLite free pages, journals, application tables, and backups use additional space. Purging delivered rows also removes their durable deduplication history; never purge earlier than the business replay window.
- Notification content and recipient identifiers remain in application memory while being sent; provider privacy terms remain the operator's responsibility.
- Retries can duplicate an external effect when a provider accepts work but the acknowledgement is lost. Callers should supply idempotency keys and providers should deduplicate when possible.

## Distribution and sustainability

The simplest distribution path is a pure-Python wheel and source distribution built from `pyproject.toml`, published to PyPI through the protected GitHub Release workflow after the owner configures Trusted Publishing. The package itself needs no hosted infrastructure. MPL-2.0 keeps distributed modifications to covered files open while allowing broad embedding; sustainability can come from paid support, integration work, or a separately operated hosted offering. No subscription economics or validated demand are assumed.

Runtime cost is provider-driven. Direct attempts are bounded by `N * (1 + R)` (default `R=3`, concurrency 10). Durable attempts are bounded by queued notifications times `max_delivery_attempts` (default 5), with no nested dispatcher retries; explicit operator requeue starts a new budget. Tests and examples make no paid provider calls.
