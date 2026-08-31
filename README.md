# Samsarix Notifications

Samsarix Notifications is a small, local-first Python library from Samsarix LLC for delivering email and JSON webhooks from an existing application. It gives application developers one async dispatch interface, explicit delivery results, bounded retries and concurrency, safe webhook destination defaults, injectable transports, and an optional crash-safe SQLite outbox.

It is not a hosted notification platform, subscriber-preference service, or general distributed task queue. Version `0.1.0` is a release candidate intended for real evaluation and is not yet published on PyPI.

## Who it is for

Use this package when a Python service or automation needs a dependable embedded notification boundary without operating another service. Direct delivery adds no database; applications that need crash recovery can opt into the standard-library SQLite outbox. If you need hundreds of provider integrations, a visual workflow editor, hosted preference management, or a high-throughput distributed broker, a mature platform such as Apprise, Courier, or Novu is a better fit.

## Requirements and installation

- Python 3.10 or newer
- An SMTP server for email, and/or an HTTPS endpoint for webhooks

Install a source checkout for evaluation:

```bash
git clone https://github.com/Deathcharge/samsarix-notifications.git
cd samsarix-notifications
python -m venv .venv
```

Activate the environment:

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Then install the package:

```bash
python -m pip install -e .
```

The package has one runtime dependency, HTTPX. SMTP delivery uses Python's standard library. The project is not currently published on PyPI, so `pip install samsarix-notifications` is not yet an advertised installation path.

## Five-minute webhook journey

Webhook delivery requires an exact host allowlist and permits only public HTTPS destinations on port 443 by default.

```python
import asyncio

from samsarix_notifications import (
    NotificationChannel,
    NotificationPayload,
    NotificationService,
    WebhookPolicy,
    WebhookRouter,
)


async def main() -> None:
    webhook = WebhookRouter(policy=WebhookPolicy(allowed_hosts=frozenset({"hooks.example.com"})))
    payload = NotificationPayload(
        channel=NotificationChannel.WEBHOOK,
        recipient="https://hooks.example.com/builds",
        subject="Build finished",
        body="release-2026.07 is ready",
        metadata={"event_type": "build.finished", "commit": "abc123"},
        idempotency_key="build-abc123",
    )

    async with NotificationService(transports={"webhook": webhook}) as notifications:
        result = await notifications.send(payload)

    if not result:
        raise RuntimeError(f"{result.error_code}: {result.error_message}")
    print(result.status.value, result.attempts, result.provider_status)


asyncio.run(main())
```

For a credential-free end-to-end demonstration against a temporary localhost receiver, run:

```bash
python examples/local_webhook.py
```

That example explicitly opts into private HTTP destinations for local development. Do not copy that policy into a server deployment.

## Durable delivery without another service

`SQLiteOutbox` persists a notification before network delivery. `OutboxWorker` uses leases so multiple local processes can safely cooperate, recovers work after an expired lease, reschedules retryable failures, and moves permanent or exhausted failures to a dead-letter state.

```python
from samsarix_notifications import NotificationPayload, SQLiteOutbox

outbox = SQLiteOutbox("application.sqlite3")
queued = outbox.enqueue(
    NotificationPayload(
        channel="email",
        recipient="customer@example.com",
        subject="Order confirmed",
        body="Order 456 has been confirmed.",
        metadata={"order_id": 456},
        idempotency_key="order-456-confirmation",
    )
)
print(queued.created, queued.message.status.value)
```

When application data uses the same SQLite database, `enqueue(..., connection=connection)` can participate in the caller's transaction. This implements the transactional-outbox boundary without making a network call while business data is locked. See [the durable outbox guide](docs/OUTBOX.md) and run `python examples/durable_outbox.py` for a complete credential-free order example.

That example uses real localhost HTTP and two separate worker processes: rollback, commit, HTTP 503 with `Retry-After`, a persisted retry deadline, restart, HTTP 202, receipt verification, and durable duplicate detection. A versioned JSON fixture checks the consumer event shape, and CI repeats the journey using an installed wheel. It is a reference consumer, not a claim of production adoption.

The outbox defaults to 10,000 retained messages across all states. Set `max_messages` for your storage budget and schedule delivered-record retention; a full queue raises `outbox_capacity_reached` without dropping existing work. This is not a disk-byte quota. See the guide for retention, replay-window, and privacy implications.

## SMTP email

```python
import asyncio
import os

from samsarix_notifications import EmailService, SMTPConfig


async def main() -> None:
    email = EmailService(
        SMTPConfig(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            from_address=os.environ["SMTP_FROM"],
            start_tls=True,
        )
    )
    result = await email.send(
        "recipient@example.com",
        "Deployment complete",
        "Version 2026.07 was deployed successfully.",
    )
    print(result.provider_status)


asyncio.run(main())
```

`EmailService` also supports bounded in-memory attachments and `$name`-style templates through `EmailTemplate`. Templates perform string substitution only; they do not evaluate expressions.

## Configuration values

Counts, capacities, byte limits, ports, and retry numbers require native Python `int` values within their documented bounds. Durations accept finite native `int` or `float` values, including fractional seconds. Boolean settings (`start_tls`, `use_ssl`, `allow_private_addresses`, and outbox `initialize`) require `True` or `False`.

Numeric strings, booleans used as numbers, fractional counts, `NaN`, infinity, and custom numeric objects are rejected; configuration mappings follow the same rules as keyword arguments. Parse environment variables explicitly (`int(...)` for counts/ports, `float(...)` for durations, and an explicit accepted-values parser for boolean flags). Do not use `bool("false")`, which is `True` in Python. Earlier pre-release dispatcher mappings silently converted some counts; that coercion is no longer supported.

Invalid numeric/boolean transport settings raise `ConfigurationError` (`invalid_configuration`); invalid dispatcher, retry, alert, or outbox limits raise `NotificationValidationError` (`invalid_input`). These numeric/boolean checks run at construction or operation entry, before transport attempts or database access. Existing bounds and defaults are unchanged: zero retries, zero retained history/cache, and zero retry delays remain available where supported. `RetryPolicy.delay_before_retry()` takes an integer retry number from 1 to 10.

## Delivery semantics

`NotificationService.send()` returns a `DeliveryResult`; it does not report success until a transport accepts the message. Results include attempts, timestamps, provider status, and stable error codes. They are truthy only when delivered.

- Retryable timeouts, connection errors, HTTP `408`/`425`/`429`, HTTP `5xx`, and transient SMTP responses use deterministic exponential backoff. Valid webhook `Retry-After` hints set a minimum delay, never an earlier clamped retry.
- Retries are capped at three by default and ten by hard validation. At worst, one request performs `1 + max_retries` provider attempts.
- Batches are capped at 1,000 payloads by default and 10,000 by hard validation; transport concurrency remains independently bounded.
- A supplied idempotency key deduplicates matching successful and in-flight sends within one `NotificationService` process. Reusing a retained key for different delivery intent returns non-retryable `idempotency_conflict` with zero transport attempts.
- Accepted running and queued deliveries are capped separately by `max_pending_deliveries` (default 1,000). At capacity, a new operation returns retryable `service_busy` with zero attempts; a matching in-flight or cached idempotent request can still reuse its result.
- Delivery results expose whether a final failure remains retryable, allowing a durable caller to make an explicit reschedule decision.
- Direct-delivery history and idempotency caches are bounded and process-local. `SQLiteOutbox` adds optional crash-safe, cross-process persistence with at-least-once delivery semantics.
- Batch results preserve input order and duplicate recipients.
- Cancellation propagates for ordinary sends. An in-flight idempotent send is shielded so a cancelled waiter cannot cause another caller to repeat an ambiguous external side effect.
- Completed idempotent operations move into the bounded success cache and release their task references even when every waiter has been cancelled. Failed or cancelled operations release the key for a later explicit retry.

### Provider-directed retry delays

Webhook failures preserve `Retry-After` as `DeliveryError.retry_after_seconds` and in the final `DeliveryResult`/`as_dict()` output. Integer-second headers and recognized HTTP dates are supported; past dates mean zero delay. Custom transports can raise `DeliveryError(..., retryable=True, retry_after_seconds=30)` using a finite native number from 0 to 86,400 seconds. Successes and failures without a hint use `None`.

Direct delivery waits for the greater of the exponential backoff and provider hint. If that wait exceeds `RetryPolicy.max_delay_seconds`, it returns a retryable failure immediately, even if retries remain; it does **not** shorten the provider's minimum or hold the request for an unbounded wait. A caller-owned scheduler can conservatively schedule no earlier than `result.completed_at + timedelta(seconds=result.retry_after_seconds)`. The outbox does this automatically using its own backoff and persists the later `available_at`, without sleeping inside the worker or blocking other ready messages.

Malformed headers of at most 128 characters fall back to local backoff. A header longer than 128 characters or a recognized delay beyond one day stops automatic retries with non-retryable `webhook_retry_after_unsupported`; durable delivery dead-letters it for operator review. This means the requested schedule is unsupported, not that the provider is permanently unavailable. Raw header values are not copied into errors.

These are **per-message** delays, not a host-, tenant-, or provider-wide rate limiter. A new direct call or another queued message is not paused by a previous failure. Applications must coordinate shared quotas and respect hints when resubmitting; keep the host clock synchronized for date-based hints and durable deadlines. Existing retry budgets, lease fencing, and at-least-once limitations still apply.

### Idempotency and payload ownership

Keys are scoped to one service instance, not automatically to a tenant, channel, or recipient. Include your application/tenant/operation scope in the key and never use one key for different intended notifications. While a key is in flight or retained in the success cache, the dispatcher compares channel, recipient, subject, body, priority, metadata, `retry_count`, and `max_retries`. A conflict does not wait for or replace the original operation, refresh its cache position, or expose its result. Fix the caller's intent/key mismatch instead of blindly retrying the conflict.

Regenerated `notification_id` and `created_at` values do not change identity. A matching retry returns the **original** delivery result/notification ID with `deduplicated=True`. Mapping order is ignored; scalar types, list versus tuple, sequence order, and attachment contents are significant. No equivalence is inferred between an `EmailAttachment` object and a dictionary that happens to encode the same attachment.

With direct idempotency enabled, the dispatcher takes its own payload snapshot before admission/queuing, including nested metadata containers. Later caller edits cannot change that accepted delivery. Snapshot/identity values support native dictionaries with string keys, lists, tuples, strings, bytes, booleans, integers up to 4,096 bits, finite floats, `None`, and `EmailAttachment` objects. Traversal is capped at 32 levels, 10,000 visited values/keys, and a 64 MiB identity-data budget (including container bookkeeping). Transport-specific encoded-message limits still apply. Unsupported values, excessive complexity, and oversized data return zero-attempt, non-retryable `idempotency_payload_unsupported`, `idempotency_payload_too_complex`, or `idempotency_payload_too_large` respectively. Validation failures do not reserve a key.

Normalize custom metadata to these types when using direct idempotency. Calls without a key, or with `idempotency_cache_size=0`, retain custom-transport metadata support but have no dispatcher snapshot or deduplication guarantee; callers must keep their payload unchanged until completion. Custom transports must not mutate supplied payloads. The service remains single-event-loop-owned, not thread-safe.

Completed identity entries retain a SHA-256 fingerprint and result, not a second copy of message bodies or attachments. Cache eviction, shutdown, process restart, or a failed/cancelled delivery ends that key's protection; subsequent calls may attempt delivery again. This is not exactly-once provider execution. `SQLiteOutbox` retains its separate JSON-only durable fingerprint contract and at-least-once semantics.

### Application shutdown and backpressure

Keep one `NotificationService` in a single application's event loop, stop submitting work before shutdown, and always await `aclose()` (or use the async context manager). The service is not thread-safe or intended to be shared across event loops. Application request cancellation is not process-crash durability; use the SQLite outbox when work must survive a restart.

```python
async def deliver_batch(webhook, payloads):
    async with NotificationService(
        transports={"webhook": webhook},
        concurrency_limit=10,
        max_pending_deliveries=1000,
        shutdown_timeout_seconds=30,
    ) as notifications:
        # Preserve every result for the caller's bounded retry/durable policy.
        return await notifications.send_batch(payloads)
```

`pending_deliveries` reports accepted logical deliveries, including those waiting for a concurrency slot. It does not count duplicate callers awaiting the same operation, and it is not a byte quota. The host should also bound request concurrency and content sizes. `max_pending_deliveries` must be an integer from 1 to 100,000.

Shutdown immediately rejects new sends with `service_closed`, allows accepted work to drain, and only then closes each currently registered unique transport. All shutdown callers await the same cleanup, even if an earlier waiter was cancelled. Drain and transport-cleanup phases each get `shutdown_timeout_seconds` (greater than zero, at most 300; default 30). When a phase expires, remaining tasks are cancelled and `aclose()` raises `NotificationError` with code `shutdown_timeout`. A failing closer does not prevent the other closers from being attempted; failures are reported as `transport_close_failed` without provider exception content. Repeating `aclose()` observes the same outcome rather than closing transports twice.

These are cooperative grace periods, not process-kill guarantees: custom `send`/`aclose` methods must avoid blocking the event loop and must propagate cancellation after cleanup. Python may wait beyond a timeout for cancellation to finish; see [asyncio cancellation and timeouts](https://docs.python.org/3/library/asyncio-task.html#timeouts). SMTP work already running in a thread cannot be forcibly stopped by cancelling an async waiter. A timeout or cancellation can therefore leave an ambiguous external effect—never infer that nothing was delivered or retry blindly. Completed-cache entries are cleared on close; bounded result history remains available for inspection.

Direct idempotency keys are scoped to the service instance and do not compare payload content. Use distinct keys for different tenants, recipients, channels, or operations; do not reuse one key for changed content. The durable outbox separately checks semantic fingerprints and rejects conflicting reuse.

Register a custom channel by implementing one async method:

```python
from samsarix_notifications import NotificationPayload, TransportResult


class AuditTransport:
    async def send(self, payload: NotificationPayload) -> TransportResult:
        # Persist the event using your application's existing durable store.
        return TransportResult(provider_status="stored")
```

Then pass `transports={"audit": AuditTransport()}` and use `channel="audit"` in the payload.

## Webhook security model

The default webhook policy:

- allows HTTPS only;
- allows port 443 only;
- requires an exact operator-supplied host allowlist;
- rejects embedded URL credentials and fragments;
- resolves the hostname before every request and rejects any non-global address;
- does not follow redirects;
- ignores ambient proxy environment variables;
- applies payload, timeout, and connection limits;
- bounds JSON traversal before encoding (32 levels, 10,000 nodes/keys, 4,096-bit integers), then enforces the encoded-byte cap;
- can HMAC-sign registered routes with `X-Samsarix-Signature: sha256=...`.

DNS validation and the later network connection are separate operations, so DNS rebinding cannot be eliminated completely by an application-layer library. Exact allowlists reduce the attacker-controlled-host case; egress firewall rules remain the strongest control for high-trust deployments. See [SECURITY.md](SECURITY.md) for trust boundaries and reporting.

## Architecture and scope

- `NotificationPayload`, `DeliveryResult`, and `RetryPolicy` define the public delivery contract.
- `NotificationService` handles transport selection, concurrency, retries, idempotency, and bounded result history.
- `SQLiteOutbox` persists JSON-safe payloads, schedules delivery, coordinates process leases, records provider receipts, and retains dead letters; `OutboxWorker` connects it to `NotificationService`.
- `EmailService` builds MIME messages and performs SMTP I/O in a worker thread.
- `WebhookRouter` supports direct notifications and named event routes over HTTPX.
- `AlertSystem` is a bounded process-local active-alert registry. It intentionally does not imply durable alert persistence or escalation automation.

Email and webhooks are the only built-in transports. The reserved Discord, Slack, SMS, and push enum values remain custom-transport names, not simulated features. There is no dependency on another Samsarix repository or private service.

## Development and verification

Install development tools:

```bash
python -m pip install -e ".[dev]"
```

Run the same checks as CI:

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest --cov=samsarix_notifications --cov-report=term-missing
python examples/local_webhook.py
python examples/durable_outbox.py
python -m build
python -m twine check dist/*
```

CI runs these checks on Linux/Python 3.10–3.14, plus Windows and macOS on Python 3.14. A separate Linux/Python 3.10 job tests the declared HTTPX minimum (`0.27.0`); other jobs resolve current compatible dependencies. Every job checks dependency consistency with `pip check` and runs the full suite and both examples against a non-editable wheel in a temporary environment outside the checkout, using isolated imports. This is representative platform coverage, not every OS/Python/dependency combination.

The release workflow additionally tests the source distribution's packaged tests, examples, and fixture in a fresh job without a checkout. The repository deliberately has no application lockfile: this is a library, and compatible runtime bounds live in `pyproject.toml`. Release artifacts should be built in an isolated environment and tested after wheel installation.

## Privacy, reliability, and cost

The library does not add telemetry, analytics, or a cloud service. Direct delivery keeps notification content and credentials in the host process and configured providers. It does not log recipients, subjects, bodies, secrets, or destination URLs. Retained in-memory delivery results contain recipient identifiers but never bodies; set `history_limit=0` when even that process-local retention is inappropriate.

The optional SQLite outbox necessarily stores recipients, subjects, bodies, JSON metadata, and provider receipts in plaintext. Place its database under appropriate filesystem permissions, backup, retention, disk-encryption, and privacy controls. Do not store credentials in notification metadata.

Repository ignore rules exclude local `.env` files and `.db`/`.sqlite`/`.sqlite3` databases with their journal/WAL/SHM sidecars. An explicit `.env.example` is allowed for placeholders only; the library does not load dotenv files. Git ignore rules do not protect already tracked files, backups, logs, or files staged with `--force`. Review staged changes before sharing, and keep production data outside the source checkout.

Provider cost is controlled by the caller's provider contract. Conservative upper bounds are:

```text
direct provider attempts <= requested notifications * (1 + configured max_retries)
durable provider attempts <= queued notifications * configured max_delivery_attempts
```

The defaults cap concurrent deliveries at 10, accepted running/queued operations at 1,000, batch inputs at 1,000, and direct-send attempts at four per notification. `OutboxWorker` makes at most one provider call per durable attempt, disabling the dispatcher's inner retry loop so the worker budget bounds the total. A distributed rate limiter, provider receipt polling, subscriber preferences, and billing controls remain outside this package's scope.

## Release, support, and license

Build artifacts with `python -m build`. The [release workflow](.github/workflows/release.yml) also validates unpublished candidates on every pull request or manual run. To check the current default branch without publishing:

```bash
gh workflow run release.yml --ref main
```

Candidate validation uploads the wheel and source distribution, downloads them in a fresh job with no repository checkout, checks artifact integrity, and runs the source distribution's full tests and both consumer examples against the installed wheel. The run summary records package SHA-256 hashes, and the `python-package-distributions` artifact is retained for seven days. A successful candidate run establishes package/consumer compatibility, not live-provider acceptance or production adoption.

The publish job is skipped for pull requests and manual runs. Publication occurs only when a GitHub Release is published with a tag matching the package version, after artifact validation and owner approval in the `pypi` environment. PyPI Trusted Publishing must be configured first. Only the separate publish job receives short-lived OIDC credentials; it downloads the validated artifacts without rebuilding them and generates PyPI attestations through the official publishing action.

The project is licensed under the [Mozilla Public License 2.0](LICENSE), with copyright and contact information in [NOTICE](NOTICE). MPL-2.0 is a file-level copyleft license: distributed modifications to covered source files remain available under MPL-2.0, while a larger proprietary application may use the library without being relicensed as a whole. License and copyright notices must be preserved. This is a practical explanation, not legal advice.

For general or licensing inquiries, email [contact@samsarix.com](mailto:contact@samsarix.com). For product support or private security reports, email [support@samsarix.com](mailto:support@samsarix.com).

GitHub-compatible citation metadata is provided in [CITATION.cff](CITATION.cff). Redistributions should retain [LICENSE](LICENSE), [NOTICE](NOTICE), and the SPDX notices attached to source files.

See [docs/PRODUCTIZATION.md](docs/PRODUCTIZATION.md) for the assessment, acceptance criteria, completed work, and remaining gates, and [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.
