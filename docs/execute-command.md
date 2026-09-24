# Execute Command (Operator CLI)

AgentCore Runtime exposes [`InvokeAgentRuntimeCommand`](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntimeCommand.html) — a data-plane API that runs **one shell command inside the microVM of a runtime session** and streams stdout/stderr back. No LLM turn, no tool loop, no tokens: it is the closest thing to "SSH into the box" that AgentCore offers. See the [launch blog](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/) and the [developer guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html).

This repo wraps it in `scripts/agentcore-exec.py`, an **operator-only** CLI for post-deploy health checks, workspace inspection, and deterministic CI/E2E assertions.

> **⚠️ SECURITY — read [the security section](#security-why-this-is-operator-only) before granting anyone the permission.** A command runs as a fresh bash **with the container's full execution-role credentials**, outside every per-user boundary this project builds. There is deliberately **no chat path** to this API and there must never be one.

## What the API does

| Aspect | Behaviour |
|---|---|
| Where it runs | The **same microVM, filesystem and session** as the OpenClaw session identified by `runtimeSessionId`. `/mnt/workspace` (the live `~/.openclaw/workspace` link and the state-dir mirror from [session storage](session-storage.md)) is visible exactly as the agent left it — no sync step. |
| Input | `command` (1 byte – 64 KB, run by bash), optional `timeout` (1–3600 s, default 300). |
| Output | An event stream: `contentStart` → `contentDelta { stdout \| stderr }` (many) → `contentStop { exitCode, status }`. `status` is `COMPLETED` or `TIMED_OUT`; `exitCode -1` means a platform error. |
| Statefulness | **One-shot.** Every call is a fresh bash: no shell history, no env carry-over between calls. Chain steps with `&&`. |
| Concurrency | Non-blocking — runs alongside `InvokeAgentRuntime` traffic on the same session. |
| Limits | 25 TPS per account/region. `RetryableConflictException` (HTTP 409) while a session is spinning up or down; `ThrottlingException` when over the rate. Both are retried by the CLI with bounded exponential backoff and jitter. |
| Session id | 33–256 chars, same rules as `InvokeAgentRuntime`. Passing an id that has no live session provisions a **new** session (empty `/mnt/workspace`). |
| Logging | CloudTrail records the caller, time and source IP. The service logs the **request id and the input command** to CloudWatch. **stdout/stderr are not logged** by the service. |
| Prerequisites | Runtimes **created before 2026-03-17 must be redeployed** (re-run `scripts/deploy.sh`) before they accept commands — there is no configuration flag to flip. The microVM ships **no dev tools beyond what `bridge/Dockerfile` installs**. The runtime stage is `node:22-slim` plus `curl`, `jq` and `python3` — so bash, node and npm are present but **`git` is deliberately not** (it exists only in the build stage). Commands that need other tools require a Dockerfile change and redeploy. |

## Operator workflow

### Prerequisites

* Python 3 with a boto3/botocore that knows the operation (verified with botocore **1.43.22**; older releases that predate the April 2026 launch make the script exit with an explicit "upgrade boto3" message). `pip install -U boto3`.
* The AWS CLI does **not** expose `invoke-agent-runtime-command` yet (checked on 2.33.15) — that is why this is a boto3 script.
* Credentials for a principal holding the [IAM statement below](#iam-statement).
* `cdk.json` with `runtime_id` / `runtime_endpoint_id` populated (`deploy.sh` does this), or pass `--runtime-arn` explicitly.

### Examples

Health check in a **new throwaway session** (default when `--session-id` is omitted — it never touches a user's session):

```bash
python3 scripts/agentcore-exec.py --command 'node --version && df -h /mnt/workspace'
```

Verify the session-storage layout (local state dir, live workspace link, mirror on the mount):

```bash
python3 scripts/agentcore-exec.py \
  --command 'test -d ~/.openclaw && test -L ~/.openclaw/workspace && readlink ~/.openclaw/workspace && ls -la /mnt/workspace/.openclaw'
```

Inspect a **specific user's live session** (pass the same `ses_<user>_<hex>` id the router generates — this reads user data, so record why in your change/incident log):

```bash
python3 scripts/agentcore-exec.py \
  --session-id ses_user_9dc5386ba1124fbd_0a1b2c3d4e5f \
  --command 'du -sh /mnt/workspace/.openclaw && ls /mnt/workspace/.openclaw/agents'
```

Machine-readable output for CI (`--json` prints one object: `sessionId`, `runtimeArn`, `status`, `exitCode`, `stdout`, `stderr`, `attempts`):

```bash
python3 scripts/agentcore-exec.py --json --timeout 60 \
  --command 'test -f /mnt/workspace/.openclaw/openclaw.json && echo present' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["exitCode"]==0, d'
```

Target an explicit runtime/endpoint/region instead of `cdk.json`:

```bash
python3 scripts/agentcore-exec.py --region us-west-2 \
  --runtime-arn arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/openclaw_agent-XXXX \
  --qualifier DEFAULT --command 'cat /etc/os-release'
```

### Exit status

| Exit | Meaning |
|---|---|
| `n` | The remote command's own exit code (`status=COMPLETED`). |
| `124` | Remote command `TIMED_OUT` (hit `--timeout`; the service killed it). Same convention as coreutils `timeout`. |
| `125` | Service reported a platform error (`exitCode -1`). |
| `1` | AWS/API error — the command did not run, or its outcome is unknown. |
| `2` | Invalid arguments (empty or >64 KB command, bad timeout/session id, no runtime target). |

### Retry semantics

`RetryableConflictException` and `ThrottlingException` are retried up to `--max-attempts` (default 6) with full-jitter exponential backoff capped at 20 s. Retries happen **only before any output has been received** — once `contentStart`/`contentDelta` has arrived the command may already have run, and re-running an arbitrary command is not safe, so the CLI fails with exit `1` instead. Every other error (`AccessDeniedException`, `ResourceNotFoundException`, `ValidationException`, …) is surfaced once, with a hint.

## IAM statement

There is **no operator or E2E role in the CDK stacks** — `scripts/deploy.sh` and `tests/e2e/` run with whatever credentials the operator already has (the same principal that runs `cdk deploy`). Attach the statement below to **that** principal (your IAM role/user, or the CI role that runs the E2E suite). Substitute region, account and the `runtime_id` from `cdk.json`:

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

Because the command runs in the same microVM as the session, it is the most direct way to verify what [session storage](session-storage.md) actually holds: whether `~/.openclaw/workspace` is the expected symlink and the mirror was restored, whether a resumed session skipped the S3 restore, or how large a workspace has grown against the 1 GB cap. Pass a router-generated `ses_…` id to look at that user's persistent mount; omit it to get an isolated, empty workspace for pure health checks.

## Testing

`tests/test_agentcore_exec.py` exercises stream parsing, exit-code mapping (`COMPLETED` vs `TIMED_OUT` vs platform error), retry/backoff behaviour and argument validation against a **mocked boto3 client** — zero AWS calls, zero model tokens:

```bash
python3 -m unittest tests.test_agentcore_exec -v
```

A live smoke test is cheap and deterministic (billed as runtime compute, not tokens): `python3 scripts/agentcore-exec.py --command 'node --version'`.
