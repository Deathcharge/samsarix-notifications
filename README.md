# Samsarix Notifications

Samsarix Notifications is a small, local-first Python library from Samsarix LLC for delivering email and JSON webhooks from an existing application. It gives application developers one async dispatch interface, explicit delivery results, bounded retries and concurrency, process-local idempotency, safe webhook destination defaults, and injectable transports for testing or additional channels.

It is not a hosted notification platform, durable queue, or user-preference service. Version `0.1.0` is a release candidate intended for real evaluation and is not yet published on PyPI.

## Who it is for

Use this package when a Python service or automation needs a dependable embedded notification boundary without adding a database or operating another service. If you need hundreds of provider integrations, a visual workflow editor, hosted preference management, or cross-process delivery guarantees, a mature platform such as Apprise, Courier, or Novu is a better fit.

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

## Delivery semantics

`NotificationService.send()` returns a `DeliveryResult`; it does not report success until a transport accepts the message. Results include attempts, timestamps, provider status, and stable error codes. They are truthy only when delivered.

- Retryable timeouts, connection errors, HTTP `408`/`425`/`429`, HTTP `5xx`, and transient SMTP responses use deterministic exponential backoff.
- Retries are capped at three by default and ten by hard validation. At worst, one request performs `1 + max_retries` provider attempts.
- Batches are capped at 1,000 payloads by default and 10,000 by hard validation; transport concurrency remains independently bounded.
- A supplied idempotency key deduplicates successful and in-flight sends within one `NotificationService` process.
- Delivery history and idempotency caches are bounded and process-local. They are not crash-safe persistence.
- Batch results preserve input order and duplicate recipients.
- Cancellation propagates for ordinary sends. An in-flight idempotent send is shielded so a cancelled waiter cannot cause another caller to repeat an ambiguous external side effect.

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
- can HMAC-sign registered routes with `X-Samsarix-Signature: sha256=...`.

DNS validation and the later network connection are separate operations, so DNS rebinding cannot be eliminated completely by an application-layer library. Exact allowlists reduce the attacker-controlled-host case; egress firewall rules remain the strongest control for high-trust deployments. See [SECURITY.md](SECURITY.md) for trust boundaries and reporting.

## Architecture and scope

- `NotificationPayload`, `DeliveryResult`, and `RetryPolicy` define the public delivery contract.
- `NotificationService` handles transport selection, concurrency, retries, idempotency, and bounded result history.
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
python -m build
python -m twine check dist/*
```

CI runs these checks on Python 3.10 through 3.14. The repository deliberately has no application lockfile: this is a library, and compatible runtime bounds live in `pyproject.toml`. Release artifacts should be built in an isolated environment and smoke-tested after wheel installation.

## Privacy, reliability, and cost

The library does not add telemetry, analytics, a database, or a cloud service. Notification content and credentials remain in the host process and configured providers. It does not log recipients, subjects, bodies, secrets, or destination URLs. Retained delivery results contain recipient identifiers but never bodies; set `history_limit=0` when even that process-local retention is inappropriate.

Provider cost is controlled by the caller's provider contract. A conservative upper-bound formula is:

```text
provider attempts <= requested notifications * (1 + configured max_retries)
```

The defaults cap concurrent deliveries at 10, batch inputs at 1,000, and total attempts at four per notification. A durable queue, distributed rate limiter, provider receipt polling, subscriber preferences, and billing controls remain outside this package's scope.

## Release, support, and license

Build artifacts with `python -m build`. Publishing is isolated in `.github/workflows/release.yml` and occurs only when a GitHub Release is published, after the `pypi` environment and PyPI Trusted Publisher are configured. The workflow uses short-lived OIDC credentials rather than a stored package token and generates PyPI attestations through the official publishing action.

The project is licensed under the [Mozilla Public License 2.0](LICENSE), with copyright and contact information in [NOTICE](NOTICE). MPL-2.0 is a file-level copyleft license: distributed modifications to covered source files remain available under MPL-2.0, while a larger proprietary application may use the library without being relicensed as a whole. License and copyright notices must be preserved. This is a practical explanation, not legal advice.

For general or licensing inquiries, email [contact@samsarix.com](mailto:contact@samsarix.com). For product support or private security reports, email [support@samsarix.com](mailto:support@samsarix.com).

GitHub-compatible citation metadata is provided in [CITATION.cff](CITATION.cff). Redistributions should retain [LICENSE](LICENSE), [NOTICE](NOTICE), and the SPDX notices attached to source files.

See [docs/PRODUCTIZATION.md](docs/PRODUCTIZATION.md) for the assessment, acceptance criteria, completed work, and remaining gates, and [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.
