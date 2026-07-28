# Security policy

## Supported versions

The unreleased `0.1.x` line is the only version currently receiving security fixes. There are no published package releases yet.

## Reporting

Please report a suspected vulnerability through GitHub's private security-advisory workflow for this repository. Do not open a public issue containing credentials, private notification content, exploit details, or production endpoints. Include the affected version or commit, the smallest reproducible input, impact, and any relevant deployment assumptions.

## Trust boundaries and invariants

Helix Notifications is an embedded library, not an authorization service. The host application decides who may send a notification and what content they may supply. The library is responsible for:

- truthful acknowledgement of transport acceptance;
- bounded retries, timeouts, concurrency, payloads, attachments, caches, and history;
- protecting SMTP and webhook credentials from logs and result objects;
- refusing SMTP authentication over plaintext;
- validating recipient and destination inputs;
- requiring exact webhook host allowlists, blocking non-public destinations, and never following redirects by default;
- avoiding expression-capable template evaluation;
- keeping external side effects explicit and testable.

Network egress controls are recommended for server deployments. DNS rebinding remains a residual risk because application-level DNS validation and the network client's connection are separate operations. Exact host allowlists are mandatory for webhook delivery.

The package stores no durable data and emits no telemetry. Delivery history and idempotency are bounded, process-local features; applications requiring crash-safe delivery must persist work before invoking the package.

## Attack surfaces and attacker stories

Security-relevant assets include SMTP credentials, webhook signing secrets, recipient identifiers, message content, alert metadata, provider quotas, and the host application's availability. Inputs may originate with an untrusted end user even though the immediate caller is trusted application code. SMTP hosts, sender identity, credentials, webhook host allowlists, timeouts, and limits are operator-controlled. Custom transport code is developer-controlled and executes with the host process's privileges.

The primary attack surfaces are public payload construction, SMTP MIME and template construction, webhook URL/DNS validation and HTTP delivery, retries and idempotency, batch fan-out, process-local result retention, and the package build artifact. Important attacker stories include SSRF to internal services, credential or private-content leakage, replay or duplicate external effects, header/template injection, resource or provider-cost amplification, ambiguous success acknowledgement, and supply-chain metadata drift.

Attacks that already require arbitrary Python execution, trusted maintainer access, or operator-controlled insecure configuration are not lower-privileged remote findings unless the privilege increase itself is the issue. The embedding application remains responsible for business authorization, consent, and recipient preferences.

## Severity calibration

- **Critical:** a broadly reachable path to code execution, cross-tenant identity compromise, or disclosure of crown-jewel credentials, with a demonstrated library source-to-sink path.
- **High:** realistic SSRF with meaningful internal impact, provider-credential disclosure, cross-tenant private-message exposure, or package/update compromise.
- **Medium:** narrower authorization/integrity violations, provider-significant duplicate delivery, or remotely triggerable resource exhaustion that materially disrupts the host.
- **Low:** limited metadata exposure, contained validation failures, or reliability issues with a realistic but low-impact attacker path.

Correctness defects without a plausible lower-privileged attacker or meaningful security boundary crossing are product bugs rather than security findings.
