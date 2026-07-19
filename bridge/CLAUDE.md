# Personal Operator v1 runtime boundary

This directory is the unprivileged, provider-credential-free AgentCore/OpenClaw
bridge. It is a frozen security boundary, not an extensibility point.

The exact model-visible surface is:

- `po_file_list`
- `po_file_read`
- `po_file_write`
- `po_file_delete`
- `po_web_read`
- `po_schedule_list`
- `po_schedule_propose`
- `po_schedule_cancel_propose`
- `po_compute_run`
- `po_compute_status`

OpenClaw uses the minimal profile, explicitly denies `session_status`, loads
only `/app/plugins/personal-operator`, and has no fallback model/provider.
Dynamic MCP, ClawHub, executable skills, arbitrary plugins, browser/computer
tools, shell/process tools, and user-supplied credentials are forbidden.
AgentCore's platform command and interactive-shell APIs are separately denied
by retained policies on the runtime and release endpoint.

The runtime receives no Telegram, Google, connector, browser, approval, or
durable provider credential. After trusted session admission, the broker may
issue a short-lived AWS workspace session restricted to the exact
server-derived namespace. The local plugin can consume it, but credentials
must never reach model context, tool arguments/results, workspace content, or
logs.

`po_web_read` crosses a trusted current-request target grant; the runtime has no
standing generic web egress. Schedule operations read or create proposals only,
and scheduled turns require `externalEffects=false`. Compute operations remain
catalogued but fail closed as `ADAPTER_DISABLED`.

For every behavior change, add a failing Node test first and run the serialized
Node 24 suite:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" npm test
```

See `../README.md` and `../docs/V1-IMPLEMENTATION-EVIDENCE.md` for the complete
Personal Operator v1 boundary and current open gates.
