# Personal Operator runtime boundary

This directory contains the credential-bearing conversational runtime. Treat
its tool surface as a frozen security contract, not an extensibility point.

OpenClaw runs with the `minimal` profile and exactly these allowed tools:

- `po_file_list`
- `po_file_read`
- `po_file_write`
- `po_file_delete`

The mutable upstream `session_status` built-in is explicitly denied because it
can persist a model/provider override. The generated config exposes only the
loopback `agentcore/bedrock-agentcore` model and has no fallback.

The only enabled plugin is `personal-operator`, loaded from
`/app/plugins/personal-operator`. Its four file tools derive the S3 prefix only
from `PERSONAL_OPERATOR_WORKSPACE_PREFIX`; identity and namespace are never
accepted as tool arguments.

URL retrieval and search are deferred. Do not expose model-selected network
egress in the same runtime as workspace reads. A future reader must derive
exact targets from the current authenticated user request in trusted code.

Do not add local command execution, process control, generic filesystem access,
headless UI automation, scheduling, cross-session control, delegated workers,
arbitrary plugin installation, arbitrary MCP servers, or user-provided
credentials to this runtime. External effects belong behind typed control-plane
capabilities and approval checks. A later isolated, credential-free sandbox can
provide code execution.

Channel delivery also stays outside this runtime. The contract returns response
text to its caller; it never fetches a channel token or calls a channel API.

`POST /invocations` accepts only `status`, `warmup`, `chat`, and `snapshot`
after binding the exact internal identity and namespace. A chat that reaches
the workspace commit returns a frozen `workspaceReceipt` containing only its
committed `generation` and `manifestSha256`; the same trusted invocation replay
returns the same receipt. Failed persistence returns no receipt and quarantines
the workspace. `snapshot` performs one fair, exclusive manual commit and
returns the same receipt shape without executing model work.

For every behavior change, add a failing Node test first and run the bridge test
suite serially under Node 24:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" npm test
```
