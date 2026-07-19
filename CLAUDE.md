# Personal Operator v1 contributor guidance

This active guidance replaces the imported OpenClaw/AgentCore sample notes.
The current source of truth is `README.md`, with assurance results in
`docs/V1-IMPLEMENTATION-EVIDENCE.md` and operational gates in
`docs/OPERATIONS.md`.

Personal Operator is an invite-only Telegram and mobile-web assistant. The
unprivileged AgentCore/OpenClaw process has an exact ten-tool `po_*` catalog and
one repository-owned plugin. It receives no provider/channel/browser/
connector/approval credential. A trusted broker may issue a short-lived AWS
workspace session for one exact server-derived namespace; those bytes stay out
of model context, tool data, workspace content, and logs.

Identity, target grants, schedules, approvals, durable effect state, provider
adapters, exports/imports, deletion, and delivery live in trusted Lambda/control
plane code. Active connector and Browser Gateway composition is disabled.
Compute returns `ADAPTER_DISABLED`. Scheduled invocations require
`externalEffects=false` and admit only reads and proposals.

Do not reintroduce the imported full tool profile, ClawHub, executable skills,
dynamic MCP, arbitrary plugins, browser/computer tools, API-key management,
shell execution, runtime provider secrets, mutable runtime endpoints, or broad
network egress. AgentCore command and interactive-shell APIs must remain
explicitly denied on both the runtime and its release endpoint.

Work RED -> GREEN, run Node tests serially under Node 24, run the full local
gate before completion, and require its exact `All local checks passed.` line.
Local source/synthesis is not cloud or provider evidence. The current release
transaction remains blocked pending the reviewed replacement described in
`docs/OPERATIONS.md`.
