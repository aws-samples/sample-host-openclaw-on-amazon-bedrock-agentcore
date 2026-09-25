# AgentCore Gateway MCP tools (prototype, opt-in)

Status: **prototype behind `enable_gateway` (default `false`)**. With the flag off nothing changes:
the eight existing CloudFormation templates synthesize byte-identical to `main`, the container
image gains two unused files, and `openclaw.json` is unchanged.

Today the in-house skills (`bridge/skills/s3-user-files`, `bridge/skills/eventbridge-cron`,
`bridge/skills/api-keys`) run as **exec skills**: OpenClaw shells out to a Node script inside the
container, which calls AWS with the runtime's scoped credentials. This prototype exposes the same
capabilities as **MCP tools served by an Amazon Bedrock AgentCore Gateway**, so that

* the model calls a typed tool (`list_files`, `create_schedule`, ...) instead of composing a shell
  command;
* the tool runs in a Lambda with its own least-privilege role, not inside the user's microVM;
* the caller's identity is a **verified Cognito JWT**, checked by the Gateway and again by the
  Lambda, instead of an environment variable the shell inherits.

The exec skills stay installed and unchanged; with the flag on, both surfaces operate on the same
S3 prefixes and the same EventBridge schedules, so a file written through either is visible through
the other.

## What the flag turns on

| Layer | Change | Where |
|---|---|---|
| CDK | New stack **`OpenClawGateway`** (only instantiated when `enable_gateway` is `true`) | `app.py`, `stacks/gateway_stack.py` |
| Gateway | `AWS::BedrockAgentCore::Gateway` `openclaw-tools`, protocol MCP, `CUSTOM_JWT` inbound authorizer: discovery URL of the existing `OpenClawSecurity` user pool, `allowedClients = [CognitoProxyClientId]` (the `openclaw-proxy` app client the Bedrock proxy already authenticates each user against) | `stacks/gateway_stack.py` |
| Interceptor | REQUEST interceptor Lambda `openclaw-gateway-interceptor` with `passRequestHeaders: true`; copies the bearer JWT into the reserved tool argument `__caller_token`, overwriting anything the model sent | `lambda/gateway_tools/interceptor/` |
| Targets | Two Lambda MCP targets: **`user-files`** (`list_files`, `read_file`, `write_file`, `delete_file`) and **`schedules`** (`create_schedule`, `list_schedules`, `update_schedule`, `delete_schedule`). Tool schemas live in one JSON file consumed by both CDK and the tests | `lambda/gateway_tools/tool-schemas.json`, `s3_user_files/`, `eventbridge_cron/` |
| Bridge | When `AGENTCORE_GATEWAY_URL` is set, the contract server mints the per-user Cognito **access** token and writes `mcp.servers.agentcore` into `openclaw.json`; a timer refreshes the bearer 5 min before expiry | `bridge/gateway-mcp.js`, `bridge/cognito-token.js`, `bridge/agentcore-contract.js` |
| Deploy | Phase 1 also deploys `OpenClawGateway`; `read_cdk_outputs` reads `GatewayUrl` by exact output name with `require_cdk_output`, and Phase 2 passes `AGENTCORE_GATEWAY_URL` to the runtime only when the flag is on | `scripts/deploy.sh` |

`api-keys` is **not** ported: it manages Secrets Manager secrets and the "native file" mode writes
into the OpenClaw state directory, which only the exec skill can reach. Follow-up.

## Identity: where the namespace comes from

The whole point of the prototype is that the model **cannot choose whose files or schedules it
touches**. Identity flows like this:

```
proxy / contract server           Gateway                      Lambda target
------------------------          --------------------------   -----------------------------------
AdminInitiateAuth(actorId) ->     CUSTOM_JWT authorizer:       lib/identity.js verifies AGAIN:
  Cognito access token             iss = pool, client_id,        RS256 vs pool JWKS, iss, aud/client_id,
  (cognito:username = actorId)     exp                           exp; namespace = cognito:username
Authorization: Bearer <token>     REQUEST interceptor copies    with ":" -> "_"   (telegram:123 ->
in mcp.servers.agentcore.headers  the bearer to __caller_token  telegram_123), same as the proxy
```

* The Lambda-target contract carries **no claims**: `context.clientContext.custom` (lowercase in
  the Node runtime; the docs' Python sample reads `client_context.custom`) has only the
  `bedrockAgentCore*` routing fields and `event` is the tool's own arguments. `Authorization` can
  never be allowlisted for header propagation. The documented way for verified identity to reach a
  Lambda target is therefore a REQUEST interceptor with `passRequestHeaders`, which is what this
  stack configures.
* The tool schemas offer **no `user_id` / `namespace` / `actor_id` argument** (enforced by
  `tests/test_gateway_stack_synth.py::test_two_lambda_targets_with_declared_tools`). If the model
  sends one anyway it is ignored (`lambda/gateway_tools/tools.test.js`, "a spoofed
  user_id/namespace argument does not change the prefix"; same for `create_schedule`).
* If the model sends its own `__caller_token`, the interceptor **overwrites** it with the real
  bearer. A call with no bearer gets a JSON-RPC error from the interceptor and is never forwarded.
* The Lambda re-verifies the signature rather than trusting the interceptor, so a misconfigured
  Gateway (or a future change to interceptor semantics) cannot turn an unsigned string into an
  identity.

`user-files` uses the same object layout as the exec skill (`<namespace>/<sanitized filename>` in
`openclaw-user-files-<account>-<region>`). `schedules` uses the same schedule names
(`openclaw-<namespace>-<id>` in group `openclaw-cron`), the same executor target and the same
`USER#<internalUserId>` / `CRON#<id>` records in `openclaw-identity`, resolving `internalUserId` the
way the skill does (`CHANNEL#<actorId>` PROFILE -> `userId`). The cron executor's existing ownership
check therefore applies to Gateway-created schedules unchanged.

## IAM

| Principal | Allowed | Scoped to |
|---|---|---|
| `openclaw-gateway-userfiles` role | `s3:ListBucket`; `s3:GetObject/PutObject/DeleteObject`; `kms:Decrypt/GenerateDataKey` | the `openclaw-user-files-<acct>-<region>` bucket / its objects; the security CMK |
| `openclaw-gateway-schedules` role | `scheduler:Create/Get/Update/DeleteSchedule`; `iam:PassRole` (`iam:PassedToService = scheduler.amazonaws.com`); `dynamodb:Get/Put/Update/DeleteItem, Query`; `kms:Decrypt/GenerateDataKey/DescribeKey` (`kms:ViaService = dynamodb.<region>.amazonaws.com`) | `schedule/openclaw-cron/*`; `openclaw-cron-scheduler-role-<region>`; table `openclaw-identity` (SSE-KMS with the security CMK) |
| `openclaw-gateway-interceptor` role | logs only | |
| Gateway service role | `lambda:InvokeFunction` | exactly the three functions above; trust policy conditioned on `aws:SourceAccount` and `aws:SourceArn = gateway/*` |

Per-user isolation inside the bucket/table is enforced by the verified namespace in code (the same
model as the runtime execution role today). `dynamodb:Scan`, `s3:*` and `Resource: "*"` are asserted
absent by `test_lambda_iam_is_scoped`. cdk-nag `AwsSolutions` runs on the stack in tests with the
suppressions documented inline in `stacks/gateway_stack.py`.

## Bridge behaviour

`bridge/gateway-mcp.js` is pure config plumbing:

```json
"mcp": {
  "servers": {
    "agentcore": {
      "url": "<AGENTCORE_GATEWAY_URL>",
      "transport": "streamable-http",
      "headers": { "Authorization": "Bearer <per-user Cognito access token>" },
      "requestTimeoutMs": 30000,
      "connectionTimeoutMs": 10000,
      "supportsParallelToolCalls": true
    }
  }
}
```

This is the `mcp.servers` shape from OpenClaw 2026.9.5 (`docs/gateway/config-extensions.md`,
`docs/tools/mcp.md`; built-in OpenClaw sessions read the in-process MCP catalog). `mcp` is a
hot-reloadable key: "MCP config changes retire only changed or removed server connections ...
active runs can continue calling their tools" (`docs/gateway/configuration/hot-reload.md`).

Token lifecycle:

1. `init()` in the contract server mints the access token for the session's `actorId` with the same
   provider the proxy uses (`bridge/cognito-token.js`: `AdminGetUser`/`AdminCreateUser` +
   HMAC-derived password + `ADMIN_USER_PASSWORD_AUTH`), so both processes derive the same password.
2. `writeOpenClawConfig()` adds the block above. If Cognito is not configured or the mint fails the
   block is omitted, a warning is logged, and the exec skills keep working; the failure is non-fatal.
3. `scheduleBearerRefresh` re-mints 5 minutes before `expiresAt` (Cognito tokens last 3600 s) and
   rewrites only the `Authorization` header, atomically (tmp file + rename) so the OpenClaw config
   watcher sees a complete file. Failed refreshes retry every 60 s. The timer is stopped on `SIGTERM`.

Inside a running container: `openclaw mcp doctor agentcore --probe` (via
`scripts/agentcore-exec.py`) lists the tools the Gateway serves and proves the bearer is accepted.

## Deploy / verify / disable

```bash
# enable
#   cdk.json: "enable_gateway": true
BUILD_MODE=codebuild ./scripts/deploy.sh          # Phase 1 now includes OpenClawGateway; Phase 2 passes AGENTCORE_GATEWAY_URL

# verify
aws cloudformation describe-stacks --stack-name OpenClawGateway \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" --output text
python3 scripts/agentcore-exec.py --session-id <ses_...> --command 'openclaw mcp doctor agentcore --probe'
# container log lines to look for:
#   [contract] Gateway MCP bearer acquired for telegram:<id> (expires ...)
#   [contract] OpenClaw headless config written (mcp.servers.agentcore enabled)
#   [contract] [gateway-mcp] bearer refreshed; next refresh before ...

# disable (runtime side first, then the stack)
#   cdk.json: "enable_gateway": false
./scripts/deploy.sh                               # Phase 2 re-deploys the runtime without AGENTCORE_GATEWAY_URL
cdk destroy OpenClawGateway                       # removes Gateway, targets, interceptor, tool Lambdas, log groups
```

`cdk destroy OpenClawGateway` holds no user data: files stay in the S3 bucket and schedules stay in
the `openclaw-cron` group, both still reachable through the exec skills.

## Tests

| Test | Proves |
|---|---|
| `tests/test_gateway_stack_synth.py` (10) | flag off -> 8 stacks, no Gateway resources or outputs; flag missing behaves as off; flag on -> 9 stacks with `GatewayUrl`/`GatewayId`; CUSTOM_JWT against the pool + interceptor with `PassRequestHeaders`; two targets whose tools match the JSON and the handlers, carry `__caller_token` and no identity argument; scoped IAM; Gateway role can only invoke the three functions and use the CMK (`kms:DescribeKey`/`Decrypt`/`GenerateDataKey`/`CreateGrant`, pinned to the AgentCore service and a gateway ARN in this account); cdk-nag has no errors |
| `lambda/gateway_tools/identity.test.js` (15, RS256, real keys) | good ID and access tokens resolve; wrong issuer / audience / signature / expired / malformed are refused; namespace derivation |
| `lambda/gateway_tools/tools.test.js` (16) | each tool addresses `<namespace>/...` from the token only; spoofed `user_id`/`namespace` ignored; interceptor overwrites `__caller_token` and refuses calls with no bearer |
| `bridge/gateway-mcp.test.js` (12) | no env var -> config untouched; env var -> exact `mcp.servers.agentcore` block; `rewriteBearer` atomic + idempotent; refresh scheduled at `expiresAt - 5 min`, forces a fresh token, retries on failure, stops cleanly |

Byte-identity of the eight existing templates with the flag off is checked by two hermetic synths
(`main` vs branch, dummy account) diffed file by file; see the PR description.

Run: `pytest tests/test_gateway_stack_synth.py -v`; `cd bridge && node --test gateway-mcp.test.js`;
`node --test lambda/gateway_tools/*.test.js` (Node 24).

## Performance (us-west-2 staging, 2026-09-24)

Same harness and prompts for both phases, 10 cold-start iterations each (session record deleted and
runtime session stopped before every iteration). Wall latency = webhook POST to the router's
`Response to send` log line; tool round trip = container `embedded run tool start` to `tool end`.
Baseline: runtime with `enable_gateway=false` (exec skills). Gateway: the rebuilt image with
`AGENTCORE_GATEWAY_URL` set, measured after the `clientContext.custom` fix below.

| Measure | exec skills p50 / p95 | Gateway tools p50 / p95 | Delta p50 |
|---|---|---|---|
| Cold start -> first reply (warm-up shim) | 4.61 s / 10.96 s | 5.41 s / 5.87 s | +0.80 s (baseline p95 was one 15 s outlier) |
| Boot -> OpenClaw 2.0 ready | 10.25 s / 15.26 s | 10.59 s / 10.80 s | +0.34 s (bearer fetch + config write ~0.5 s of boot) |
| First tool call after cold start (wall) | 7.80 s / 9.38 s | 8.21 s / 9.52 s | +0.41 s |
| Plain chat reply (wall) | 3.42 s / 4.54 s | 3.69 s / 4.33 s | +0.27 s (larger tool list in every prompt) |
| Repeat tool prompt (wall) | 5.50 s / 6.57 s | 4.74 s / 7.03 s | -0.76 s (5/10 repeats answered from context without re-calling) |
| Tool round trip, first call | 483 ms / 627 ms | 300 ms / 323 ms | -183 ms (Lambda vs spawning `node` for the exec skill) |
| Tool round trip, repeat call | 223 ms / 258 ms | 267 ms / 309 ms | +44 ms (warm exec process beats a second Gateway hop) |
| Tools forwarded per model call / median input tokens | 38 / 27.2k | 51 / 29.0k | +13 tools / +1.8k (+6.6 %) |
| Correct reply (first / repeat) | 10/10 / 10/10 | 10/10 / 10/10 | 0 |
| Timeouts / errors | 0 / 0 | 0 / 0 | 0 |

Net: chat and startup latency move by well under a second at p50, within the run-to-run noise of a
10-sample set; the tool call itself is faster through the Gateway on a cold process and slightly
slower than a warm exec process; every prompt carries ~1.8k more input tokens because 13 more tool
definitions ride along. The model re-called the tool on a repeated prompt less often with the typed
MCP tools (5/10 vs 9/10), answering from the earlier structured result instead.

E2E on the same deployment: `tests/e2e/test_gateway_tools.py -m gateway` 4/4 after the Lambda fixes
below; `full_startup` 1/1, `smoke` 4/4, `lifecycle` 1/1 unchanged against `main`.

## Questions settled by the live E2E (us-west-2 staging, 2026-09-24)

1. **Does the Gateway accept the Cognito ID token, or only an access token? Only the access token.**
   With `allowedClients = [<client id>]`, an `initialize` POST carrying the ID token (`token_use=id`,
   `aud=<client id>`) is refused `403 {"code":-32002,"message":"insufficient_scope - The request
   requires higher privileges than provided by the access token."}`; the same user's access token
   (`token_use=access`, `client_id=<client id>`, `scope=aws.cognito.signin.user.admin`) gets `200`
   and `tools/list` returns the two targets' tools. The first image built with the ID token showed
   this in the container log as `[bundle-mcp] failed to start server "agentcore" ...: Streamable HTTP
   error: Error POSTing to endpoint` and OpenClaw fell back to the exec skill. `bridge/cognito-token.js`
   therefore exposes `getAccessToken()` (same `ADMIN_USER_PASSWORD_AUTH` call, both tokens cached
   together) and the contract server uses it for the MCP bearer; the proxy keeps using the ID token
   for its own purposes. `lib/identity.js` in the Lambdas already verified either type.
2. **Does a bearer refresh break an in-flight tool call? No.** The Gateway's Streamable-HTTP
   endpoint is stateless per request: `initialize` returns no `Mcp-Session-Id`, and every
   `tools/call` is authorised on the bearer it carries. Live probe: call with token A -> 200; mint
   token B the way the refresh does; the same call with B -> 200; with A again -> 200 (issuing B does
   not revoke A, which stays valid until its own `exp`); fresh `initialize` + call with B -> 200.
   So a call already in flight when `openclaw.json` is rewritten completes on the request it sent,
   and the next call carries the new header. The bridge refreshes 5 min before expiry and retries a
   failed refresh every 60 s, so the old token is still valid for the whole retry window.
3. **First live surprise:** the first deployed Lambdas read `clientContext.Custom` and got
   `tool=""` (every call answered `unknown_tool`, which the model rendered as "no files"). Fixed to
   read `custom` with `Custom` as fallback; the `gateway_tool_call` audit line now shows the tool.
4. **Second live surprise:** `create_schedule` reached the schedules Lambda with the right namespace
   but no schedule appeared: `openclaw-identity` is SSE-KMS encrypted with the security CMK and the
   role had no KMS statement, so `PutItem` failed inside the tool's error envelope. Fixed by the
   `kms:ViaService = dynamodb` grant in the IAM table above, and the Lambdas now also write a
   `gateway_tool_error` line (tool, error name, message; no arguments) so the next such failure is
   visible in CloudWatch. After both fixes `tests/e2e/test_gateway_tools.py -m gateway` passed 4/4.

All answers are recorded in the E2E report referenced from the PR.
