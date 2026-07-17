# Personal Operator runtime boundary

This directory contains the credential-bearing conversational runtime. Treat
its tool surface as a frozen security contract, not an extensibility point.

OpenClaw runs with the `minimal` profile and exactly these allowed tools:

- `session_status`
- `web_fetch`
- `po_file_list`
- `po_file_read`
- `po_file_write`
- `po_file_delete`

The only enabled plugin is `personal-operator`, loaded from
`/app/plugins/personal-operator`. Its four file tools derive the S3 prefix only
from `PERSONAL_OPERATOR_WORKSPACE_PREFIX`; identity and namespace are never
accepted as tool arguments.

Do not add local command execution, process control, generic filesystem access,
headless UI automation, scheduling, cross-session control, delegated workers,
arbitrary plugin installation, arbitrary MCP servers, or user-provided
credentials to this runtime. External effects belong behind typed control-plane
capabilities and approval checks. A later isolated, credential-free sandbox can
provide code execution.

Channel delivery also stays outside this runtime. The contract returns response
text to its caller; it never fetches a channel token or calls a channel API.

For every behavior change, add a failing Node test first and run the bridge test
suite serially under Node 24:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" npm test
```
