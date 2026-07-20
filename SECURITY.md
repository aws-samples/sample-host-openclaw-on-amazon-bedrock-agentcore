# Personal Operator v1 security boundary

Personal Operator v1 is pre-production. Use public or synthetic data only. The
current evidence and all open external gates are in
`docs/V1-IMPLEMENTATION-EVIDENCE.md`; detailed contracts are in
`docs/CAPABILITY-BOUNDARY.md`, `docs/PRIVACY-BOUNDARY.md`, and
`docs/OPERATIONS.md`.

The model runtime is provider-credential-free and unprivileged. It has no STS,
direct workspace S3, DynamoDB, Scheduler/EventBridge, Secrets Manager,
connector, browser, or compute IAM authority. A trusted broker can exchange one
admitted bearer capability for a short-lived AWS session restricted to one
workspace namespace. The local plugin consumes that session; credential bytes
must not enter model context, tool data, durable workspace state, or logs.

The effective model surface is exactly the ten catalogued `po_*` operations.
Dynamic MCP, ClawHub, arbitrary plugins/skills, browser/computer tools, and
local shell/process execution are forbidden. AgentCore's separate command and
interactive-shell APIs are denied by retained resource policies on both the
runtime and immutable release endpoint. External effects stay in the trusted
Task-3 kernel; active connector/browser composition and production compute are
disabled.

Local tests and offline synthesis do not prove live AWS IAM, networking,
storage, signing, scanning, runtime behavior, provider effects, or pilot
safety. Do not mutate AWS through the current release transaction; its
composer/phase design must be replaced and independently reviewed first.

## Reporting security issues

See [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications).
