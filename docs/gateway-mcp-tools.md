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
| Gateway | `AWS::BedrockAgentCore::Gateway` `openclaw-tools`, protocol MCP, `CUSTOM_JWT` inbound authorizer: discovery URL of the existing `OpenClawSecurity` user pool, `allowedClients = [CognitoProxyClientId]` (the `openclaw-proxy` app client whose ID token the Bedrock proxy already mints per user) | `stacks/gateway_stack.py` |
| Interceptor | REQUEST interceptor Lambda `openclaw-gateway-interceptor` with `passRequestHeaders: true`; copies the bearer JWT into the reserved tool argument `__caller_token`, overwriting anything the model sent | `lambda/gateway_tools/interceptor/` |
| Targets | Two Lambda MCP targets: **`user-files`** (`list_files`, `read_file`, `write_file`, `delete_file`) and **`schedules`** (`create_schedule`, `list_schedules`, `update_schedule`, `delete_schedule`). Tool schemas live in one JSON file consumed by both CDK and the tests | `lambda/gateway_tools/tool-schemas.json`, `s3_user_files/`, `eventbridge_cron/` |
| Bridge | When `AGENTCORE_GATEWAY_URL` is set, the contract server mints the per-user Cognito ID token and writes `mcp.servers.agentcore` into `openclaw.json`; a timer refreshes the bearer 5 min before expiry | `bridge/gateway-mcp.js`, `bridge/cognito-token.js`, `bridge/agentcore-contract.js` |
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
  Cognito ID token                 iss = pool, aud = client,     RS256 vs pool JWKS, iss, aud/client_id,
  (cognito:username = actorId)     exp                           exp; namespace = cognito:username
Authorization: Bearer <token>     REQUEST interceptor copies    with ":" -> "_"   (telegram:123 ->
in mcp.servers.agentcore.headers  the bearer to __caller_token  telegram_123), same as the proxy
```

* The Lambda-target contract carries **no claims**: `context.clientContext.Custom` has only the
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
| `openclaw-gateway-schedules` role | `scheduler:Create/Get/Update/DeleteSchedule`; `iam:PassRole` (`iam:PassedToService = scheduler.amazonaws.com`); `dynamodb:Get/Put/Update/DeleteItem, Query` | `schedule/openclaw-cron/*`; `openclaw-cron-scheduler-role-<region>`; table `openclaw-identity` |
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
      "headers": { "Authorization": "Bearer <per-user Cognito ID token>" },
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

1. `init()` in the contract server mints the ID token for the session's `actorId` with the same
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
python scripts/agentcore-exec.py --runtime-id <id> --user <ns> -- openclaw mcp doctor agentcore --probe
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
| `lambda/gateway_tools/identity.test.js` (RS256, real keys) | good ID and access tokens resolve; wrong issuer / audience / signature / expired / malformed are refused; namespace derivation |
| `lambda/gateway_tools/tools.test.js` | each tool addresses `<namespace>/...` from the token only; spoofed `user_id`/`namespace` ignored; interceptor overwrites `__caller_token` and refuses calls with no bearer |
| `bridge/gateway-mcp.test.js` (11) | no env var -> config untouched; env var -> exact `mcp.servers.agentcore` block; `rewriteBearer` atomic + idempotent; refresh scheduled at `expiresAt - 5 min`, forces a fresh token, retries on failure, stops cleanly |

Byte-identity of the eight existing templates with the flag off is checked by two hermetic synths
(`main` vs branch, dummy account) diffed file by file; see the PR description.

Run: `pytest tests/test_gateway_stack_synth.py -v`; `cd bridge && node --test gateway-mcp.test.js`;
`node --test lambda/gateway_tools/*.test.js` (Node 24).

## Open questions (to be settled by the live E2E)

1. **Does the Gateway accept the Cognito ID token, or only an access token?** The AgentCore docs
   describe `allowedClients` as matching the `client_id` claim, which only access tokens carry (ID
   tokens carry `aud`). The proxy mints ID tokens today; `lib/identity.js` accepts both so the Lambda
   side is not the blocker. If the Gateway rejects ID tokens, `bridge/cognito-token.js` switches to
   `AuthenticationResult.AccessToken` for the MCP header (one line) and the `allowedAudience` /
   `allowedClients` choice in the stack follows.
2. **Does a bearer refresh break an in-flight tool call?** The hot-reload doc says unchanged
   servers keep their connections; a changed `headers` value counts as a changed server, so the
   expectation is that the connection is retired and re-created on the next turn while the current
   run finishes on the old transport. To be confirmed with a forced refresh during a long tool call.

Both answers are recorded in the E2E report referenced from the PR.
