# Execute Command (Operator CLI)

AgentCore Runtime exposes [`InvokeAgentRuntimeCommand`](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntimeCommand.html) — a data-plane API that runs **one shell command inside the microVM of an AgentCore Runtime session** and streams stdout/stderr back. No LLM turn, no tool loop, no tokens: it is the closest thing to "SSH into the box" that AgentCore offers. See the [launch blog](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/) and the [developer guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html).

This repo wraps it in `scripts/agentcore-exec.py`, an **operator-only** CLI for post-deploy health checks, workspace inspection, and deterministic CI/E2E assertions.

> **⚠️ SECURITY — read [the security section](#security-why-this-is-operator-only) before granting anyone the permission.** A command runs as a fresh bash **with the container's full execution-role credentials**, outside every per-user boundary this project builds. There is deliberately **no chat path** to this API and there must never be one.

## What the API does

| Aspect | Behaviour |
|---|---|
| Where it runs | The **same microVM, filesystem and session** selected by AgentCore Runtime ARN, endpoint name and `runtimeSessionId`. `~/.openclaw/workspace` is a real local directory; `/mnt/workspace/.openclaw` is the [session-storage mirror](session-storage.md), which may lag current local writes. |
| Input | `command` (1 byte – 64 KB, run by bash), optional `timeout` (1–3600 s, default 300). |
| Output | An event stream: `contentStart` → `contentDelta { stdout \| stderr }` (many) → `contentStop { exitCode, status }`. `status` is `COMPLETED` or `TIMED_OUT`; `exitCode -1` means a platform error. |
| Statefulness | **One-shot.** Every call is a fresh bash: no shell history, no env carry-over between calls. Chain steps with `&&`. |
| Concurrency | Non-blocking — runs alongside `InvokeAgentRuntime` traffic on the same session. |
| Limits | 25 TPS per account/region. `RetryableConflictException` (HTTP 409) while a session is spinning up or down; `ThrottlingException` when over the rate. Both are retried by the CLI with bounded exponential backoff and jitter. |
| Session id | 33–256 chars. If omitted, the CLI generates a fresh `exec_<uuid>` id rather than selecting a user's live session. This does not bind an application user or verify restoration/activation. Live behavior of a new session was not tested in this task. |
| Logging | CloudTrail records the caller, time and source IP. The service logs the **request id and the input command** to CloudWatch. **stdout/stderr are not logged** by the service. |
| Prerequisites | Runtimes **created before 2026-03-17 must be redeployed** before they accept commands — there is no configuration flag to flip. Use the [AgentCore Runtime V2 release workflow](runtime-v2.md). The microVM ships **no dev tools beyond what `bridge/Dockerfile` installs**; the current image uses `node:24-slim`. Commands that need other tools require a Dockerfile change and redeploy. |

## Operator workflow

### Prerequisites

* Prepare `.venv-deploy` with uv as shown in the [offline setup](runtime-v2.md#offline-checks). It uses `requirements-deploy.txt`, with `boto3==1.43.95` and `botocore==1.43.95`, to run the operator script. The script rejects SDKs without the command operation.
* The AWS CLI does **not** expose `invoke-agent-runtime-command` yet (checked on 2.33.15) — that is why this is a boto3 script.
* Credentials for a principal holding the [IAM statement below](#iam-statement).
* Use the selected user's authoritative route for the AgentCore Runtime ARN, endpoint name and session ID. The deployment manifest may target a different endpoint from an existing enrolled user. The CLI resolves a V2 `--manifest` or an explicit `--runtime-arn` / `--runtime-id` with `--qualifier`; it has no legacy CDK AgentCore Runtime fallback.

### Examples

Set `ACTIVE_RUNTIME_ARN`, `ACTIVE_ENDPOINT_NAME` and `ACTIVE_SESSION_ID` from the selected user's
`USER#<userId>` / `SESSION` record (`runtimeArn`, `endpointName`, `sessionId`). Reject old or incomplete routes rather than filling defaults. Use a known
live, activated V2 session and its Region in `CDK_DEFAULT_REGION`; do not invent an ID or substitute
the latest deployment endpoint. Record the reason for inspecting user data in your incident/change log.

Check the selected live container:

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/agentcore-exec.py --region "$CDK_DEFAULT_REGION" \
  --runtime-arn "$ACTIVE_RUNTIME_ARN" --qualifier "$ACTIVE_ENDPOINT_NAME" \
  --session-id "$ACTIVE_SESSION_ID" --command 'node --version && df -h /mnt/workspace'
```

Verify the local state/workspace directories and the mount mirror. This checks layout, not
checkpoint completeness or equality with the latest local writes:

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/agentcore-exec.py --region "$CDK_DEFAULT_REGION" \
  --runtime-arn "$ACTIVE_RUNTIME_ARN" --qualifier "$ACTIVE_ENDPOINT_NAME" \
  --session-id "$ACTIVE_SESSION_ID" \
  --command 'test -d ~/.openclaw && test ! -L ~/.openclaw && test -d ~/.openclaw/workspace && test ! -L ~/.openclaw/workspace && test -d /mnt/workspace/.openclaw && ls -ld ~/.openclaw/workspace /mnt/workspace/.openclaw'
```

Inspect local state and the mirror in that same session:

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/agentcore-exec.py --region "$CDK_DEFAULT_REGION" \
  --runtime-arn "$ACTIVE_RUNTIME_ARN" --qualifier "$ACTIVE_ENDPOINT_NAME" \
  --session-id "$ACTIVE_SESSION_ID" \
  --command 'du -sh ~/.openclaw /mnt/workspace/.openclaw && ls ~/.openclaw/agents'
```

Machine-readable output for CI (`--json` prints one object: `sessionId`, `runtimeArn`, `status`, `exitCode`, `stdout`, `stderr`, `attempts`):

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/agentcore-exec.py --region "$CDK_DEFAULT_REGION" \
  --runtime-arn "$ACTIVE_RUNTIME_ARN" --qualifier "$ACTIVE_ENDPOINT_NAME" \
  --session-id "$ACTIVE_SESSION_ID" --json --timeout 60 \
  --command 'test -f ~/.openclaw/openclaw.json && echo present' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["exitCode"]==0, d'
```

Omitting `--session-id` selects a new container session, not the active user's workspace.

### Exit status

| Exit | Meaning |
|---|---|
| `n` | The remote command's own exit code (`status=COMPLETED`). |
| `124` | Remote command `TIMED_OUT` (hit `--timeout`; the service killed it). Same convention as coreutils `timeout`. |
| `125` | Service reported a platform error (`exitCode -1`). |
| `1` | AWS/API error — the command did not run, or its outcome is unknown. |
| `2` | Invalid arguments (empty or >64 KB command, bad timeout/session id, no AgentCore Runtime target). |

### Retry semantics

`RetryableConflictException` and `ThrottlingException` are retried up to `--max-attempts` (default 6) with full-jitter exponential backoff capped at 20 s. Retries happen **only before any output has been received** — once `contentStart`/`contentDelta` has arrived the command may already have run, and re-running an arbitrary command is not safe, so the CLI fails with exit `1` instead. Every other error (`AccessDeniedException`, `ResourceNotFoundException`, `ValidationException`, …) is surfaced once, with a hint.

## IAM statement

There is **no operator or E2E role in the CDK stacks** — `scripts/deploy.sh` and `tests/e2e/` run with whatever credentials the operator already has (the same principal that runs `cdk deploy`). Attach the statement below to **that** principal (your IAM role/user, or the CI role that runs the E2E suite). Substitute the Region, account and AgentCore Runtime ID from the selected route's ARN:

```json
{
  "Sid": "AgentCoreOperatorExecuteCommand",
  "Effect": "Allow",
  "Action": "bedrock-agentcore:InvokeAgentRuntimeCommand",
  "Resource": [
    "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:runtime/RUNTIME_ID",
    "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:runtime/RUNTIME_ID/*"
  ]
}
```

The `/*` resource mirrors the existing `InvokeAgentRuntime` grants in `stacks/router_stack.py` and `stacks/cron_stack.py`: IAM evaluates against `runtime/{id}/runtime-endpoint/{endpoint}`. If you compose the ARN from `--runtime-id` (the default), the script also calls `sts:GetCallerIdentity`, which every principal can call.

**Do not** add this action to:

* the **router Lambda role** (`OpenClawRouter`) — that would turn any Telegram/Slack/Feishu message into a potential root-shell path;
* the **cron Lambda role** (`OpenClawCron`) — same exposure via scheduled payloads;
* the **AgentCore execution role** itself.

If a dedicated operator role is wanted later, it belongs in a separate, deliberate CDK change with its own cdk-nag `AwsSolutions-IAM5` suppression for the `/*` resource (see the suppression pattern in `stacks/router_stack.py`).

## Security: why this is operator-only

OpenClaw's own `exec` tool already gives the agent a shell — but a **contained** one. On container init `bridge/agentcore-contract.js` (running as the execution role) calls `bridge/scoped-credentials.js`, which assumes the execution role with a **per-user STS session policy** (S3 prefix, per-user secrets, DynamoDB leading keys, …), strips every AWS credential variable from OpenClaw's environment, and hands OpenClaw a `credential_process` that only yields those scoped credentials. Every model turn that decides to run a command also passes through **Bedrock Guardrails** and OpenClaw's tool policy. See [docs/security.md → STS Session-Scoped Credentials](security.md).

`InvokeAgentRuntimeCommand` sits **outside all of that**:

```
                     ┌─────────────────────────── microVM ───────────────────────────┐
 chat message ──▶ router ──▶ OpenClaw (LLM) ──▶ exec tool ──▶ bash  [SCOPED creds,   │
                    guardrails ✓   tool policy ✓             credential_process only] │
                                                                                      │
 operator ──▶ InvokeAgentRuntimeCommand ─────────────────▶ fresh bash  [FULL execution-│
                    guardrails ✗   tool policy ✗   scoped-credentials.js ✗   role creds]│
                     └────────────────────────────────────────────────────────────────┘
```

Concretely, a command sent through this API:

1. **Runs with the full execution-role credentials.** The fresh bash inherits `AWS_CONTAINER_CREDENTIALS_*` from the container, so it can reach every S3 prefix, every `openclaw/user/*` secret, DynamoDB, EventBridge Scheduler, KMS and STS exactly as the execution role can — not just one user's slice.
2. **Bypasses `scoped-credentials.js`.** The session-policy narrowing, the credential env blocklist and the zero-access fallback all apply to the OpenClaw child process. This bash is not that process.
3. **Bypasses Bedrock Guardrails and OpenClaw's tool policy.** No model is involved, so nothing evaluates the command's intent.
4. **Can read and modify the live session's `/mnt/workspace`**, including another user's conversation history, memory and credentials files if you pass their session id.

That is why:

* Only **human operators and CI/E2E principals** get `bedrock-agentcore:InvokeAgentRuntimeCommand`. The router and cron roles do not, and nothing in `lambda/` calls this API.
* **No chat surface exists** for it — no `/status`, `/git-pull` or similar slash commands — and none should be added. Any future chat-triggered variant would need a fixed, server-side allowlist of named commands with **zero free-form user input**, and would still widen the blast radius of a compromised router; treat that as a separate security review, not a follow-up PR.
* The CLI never builds a command string from anything but `--command` (no interpolation of environment, arguments or files), refuses empty and over-64 KB input, and defaults to a **throwaway session** so accidental reads of user data require an explicit `--session-id`.
* Every invocation is attributable in CloudTrail; log the reason when you target a real user's session.

## Relationship to session storage

Because the command runs in the same microVM as the session, it is the most direct way to verify what [session storage](session-storage.md) actually holds: whether the mirror was restored to the local state dir, which files a resumed session filled in from S3, or how large a workspace has grown against the 1 GB cap. Pass a router-generated `ses_…` id to look at that user's persistent mount; omit it to get an isolated, empty workspace for pure health checks.

## Testing

`tests/test_agentcore_exec.py` exercises stream parsing, exit-code mapping (`COMPLETED` vs `TIMED_OUT` vs platform error), retry/backoff behaviour and argument validation against a **mocked boto3 client** — zero AWS calls, zero model tokens:

```bash
python3 -m unittest tests.test_agentcore_exec -v
```

A live smoke test is cheap and deterministic (billed as AgentCore Runtime compute, not tokens): `uv run --no-project --python .venv-deploy/bin/python python scripts/agentcore-exec.py --command 'node --version'`.
