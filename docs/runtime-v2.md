# Amazon Bedrock AgentCore Runtime V2

AgentCore Runtime V2 prepares your agent environment once, takes a snapshot, and restores it for each new instance. The `platformVersion` setting controls this startup behavior and is separate from the versions that track configuration history. V1 is the default for new agent runtimes.

- Consistent cold starts: snapshot restore keeps cold-start latency fast and consistent regardless of concurrency or image size.
- Lower cost for always-on or bursty agents: billing follows active resource use, while AgentCore Runtime reclaims memory as the agent releases it and reduces platform overhead.

This image prepares a user-neutral OpenClaw gateway; user state is restored during activation.

The pinned SDK controller explicitly sets `platformVersion="V2"`; CDK manages foundation and dependent stacks. [AWS documents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions): "AWS CloudFormation and the AWS CDK do not currently support setting platformVersion."

## Lifecycle and compatibility

Before healthy `/ping`, bootstrap prepares the live gateway without user state, scoped credentials, external Browser sessions or user model/tool calls. Activation binds the user, acquires credentials and strictly restores state and skills. Eligible empty-state sessions bind the prepared gateway; restored or custom state uses normal gateway startup. Chat and cron wait for `USER_READY`: credentials, proxy, gateway, configured Browser and persistence must all be ready.

Restore failure, partial failure or timeout blocks activation and uploads. Credentials and gateway/Browser readiness are checked before dispatch. SQLite stays on local disk, mirrored to session storage and backed up to S3; never open its databases directly on the mount. See [state storage](session-storage.md) and [runtime behavior](how-it-works.md).

OpenClaw `2026.9.5` keeps its generated Runtime metadata in system messages while Guardrails assess user input. Preserve this boundary when changing providers. Source patches verify package versions and exact hashes; upgrades require rechecking patches, initialization boundaries and tests. See [Guardrails](guardrails.md).

### Model and API contract

The bridge translates OpenClaw's OpenAI-compatible streaming chat to Bedrock `ConverseStream`. AgentCore Runtime callers receive JSON unless they send `action:"chat", stream:true`. `cdk.json` and the deployment wrapper select:

- Primary: `global.anthropic.claude-haiku-5-5` (`BEDROCK_MODEL_ID`), with `additionalModelRequestFields.thinking.type="disabled"` and no temperature.
- Fallback: `global.anthropic.claude-sonnet-4-6` (`BEDROCK_FALLBACK_MODEL_ID`), retaining its model-specific settings.

Users can replace the primary for their own main-agent turns with `/model <alias>` from the `user_selectable_models` allowlist (see [Per-user model switching](../README.md#per-user-model-switching)). The Router sends the stored model ID as `modelOverride` in each `action:"chat"` payload; the contract writes it into that turn's identity file, and the proxy uses it only when it is in `BEDROCK_USER_SELECTABLE_MODEL_IDS`. Sub-agents keep `SUBAGENT_BEDROCK_MODEL_ID`, the fallback stays `BEDROCK_FALLBACK_MODEL_ID` (skipped when it equals the override), and `thinking`/temperature settings follow the model actually sent.

Fallback is limited to eligible transient primary API failures before content starts. Partial output, tool execution, cancellation, Guardrail intervention and ambiguous transport outcomes must not trigger replay through another model.

The execution role grants invocation and streaming for the source-account `inference-profile/*` and cross-region `foundation-model/*` resources, covering both models. Guardrail and Marketplace grants remain in place. Verify model access and organization/Region restrictions in the deployment account.

## Offline checks

Run from the repository root with Node 24.16+ in the 24.x line, Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Use `.venv` for CDK/tests and `.venv-deploy` for the SDK controller. Installation needs package-registry access; these checks use local models, fakes and synthetic CDK context.

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -r requirements-deploy.txt pytest
uv venv .venv-deploy
uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt
npm ci --prefix bridge

uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py preflight --region us-west-2
npm test --prefix bridge
uv run --no-project --python .venv/bin/python python -m pytest -q \
  tests/test_runtime_deploy.py tests/test_runtime_routing.py tests/test_runtime_enrollment.py \
  tests/test_runtime_routing_synth.py tests/test_runtime_routing_unknown_outcome.py \
  tests/test_deploy_wrapper.py tests/test_runtime_manifest.py
```

`requirements-deploy.lock` pins `boto3==1.43.95` and `botocore==1.43.95`. Preflight rejects incompatible SDK versions/service models without calling AWS. Offline passes do not certify live deployment; record failures and skips.

## Deploy and verify V2

These commands change AWS resources and incur model charges. Confirm account/Region and install both environments above. The default manifest is `.runtime/runtime-v2.json`, passed to CDK as `runtime_manifest`. Phase 1 needs no manifest; Phase 3 requires live readback and successful chat warmup.

```bash
export CDK_DEFAULT_REGION=us-west-2
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# Fresh installation: foundation, V2 release_1, verification, dependent stacks.
./scripts/deploy.sh

# Or run the phases separately.
./scripts/deploy.sh --phase1
./scripts/deploy.sh --runtime-only --manifest .runtime/runtime-v2.json
uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py verify \
  --region "$CDK_DEFAULT_REGION" --manifest .runtime/runtime-v2.json --require-enrollment-ready
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json
```

Use the full deployment or the individual phases. Local builds use ARM64 and `ECR_REPOSITORY` (default `openclaw-bridge`); pass `--image-uri "$IMAGE_URI"` for a prebuilt ECR image whose digest is recorded.
Supplied images and `ECR_REPOSITORY` must use execution-role-accessible repositories in the STS-verified account and deployment Region with prefixes `openclaw-bridge*`, `openclaw_agent*` or `bedrock-agentcore-*`; other repositories are rejected before build, repository creation or AgentCore Runtime updates.

The initial immutable endpoint is `release_1`. For changed images/configuration, archive the previous manifest and choose an unused name such as `release_2`:

```bash
./scripts/deploy.sh --runtime-only --endpoint-name release_2 \
  --manifest .runtime/runtime-v2.json --image-uri "$IMAGE_URI"
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json
```

The controller never repoints an existing endpoint. Keep manifests, endpoints, shared Browser/Gateway/Guardrail resources and IAM permissions for existing saved routes. Unchanged deployments may reuse verified warmup evidence; this is not a fresh measurement.

### Post-deployment warmup

After READY/readback, the controller sends a real `action:"chat"` asking for `ok` with a synthetic `deployment-warmup:<uuid>` actor and new session. It incurs model cost and state writes, without real-user route records or channel delivery. `/ping` or status alone is insufficient.

Success requires the acknowledgement, no channel delivery, `runtimePlatform:"V2"`, `phase:"USER_READY"`, full `execution:{engine:"openclaw",phase:"completed"}` and matching `userId`, `actorId`, `sessionId` and `runtimeSessionId`. The manifest records this evidence with `warmup.contractVersion:1`.

The attempt is saved before Invoke. Pending, failed, unknown or skipped results block Phase 3. `--skip-warmup` is allowed only with `--runtime-only` for unverified artifact preparation. Ambiguous outcomes require [failure recovery](#failure-recovery) before another attempt.

## New registrations

Phase 3 enables genuine first registrations on the verified release. Missing channel mappings alone do not prove a new identity: retained ownership, missing SESSION/profile or incomplete routes require reconciliation.

The saved `USER#<userId>` / `SESSION` route remains authoritative after later deployments. An unowned channel can link as an alias without changing the storage actor or release; independently registered accounts cannot merge or transfer ownership. See [session inspection](operations.md#inspect-sessions).

## Existing-user migration and cutover

Existing-user migration, cutover and automatic rollback are not implemented. New endpoints and registrations do not migrate existing users. Stopping a session neither establishes a checkpoint nor changes its route; retain existing routes and dependencies.
Before dependent-stack deployment, Phase 3 uses `--check-existing-callers` to read the configuration of `openclaw-router` and `openclaw-cron-executor` without modifying Lambda or SESSION records. Absent functions or release updates retaining the same V2 ARN pass; changed ARNs, missing/invalid V2 `RUNTIME_TARGET` or unavailable configuration block deployment.

## Failure recovery

| Failure | Operator action |
|---|---|
| SDK/readback failure | Correct the pinned environment or configuration, preserve the manifest and reverify before Phase 3 |
| Warmup failure/uncertainty | Copy the manifest before another attempt; it records the current attempt, not an append-only history. Reconcile the synthetic session, tool effects and delivery first |
| Restore, credential, gateway or Browser failure | Keep the failed binding closed. Inspect state and readiness logs; do not start an empty gateway or substitute another owner |
| Missing ownership/SESSION | Reconcile identity records and their saved route; never delete SESSION or identity records as a reset |

Transport errors, deadlines and `unknown_outcome` retain admission tokens and `inFlight`, even when a decoded response otherwise claims success. Remote work may continue. Do not retry or clear retained tokens until effects and delivery are reconciled. Application logs are in `/openclaw/container`, prefixed by the saved actor with `:` replaced by `_`.

## Startup measurement points

Every `POST /invocations` unconditionally emits structured console JSON (`event:"PRE_ENTRYPOINT"`, `marker:"[PRE-ENTRYPOINT]"`, `wall_unix_ms`, `request_id`) at handler entry, before body reading/authentication, stream branching, user restore or Gateway/model processing. This application-handler marker requires neither benchmark environment flags nor `startupAttemptId` and does not measure TTFT or pure microVM startup.

`ENTRYPOINT_EXEC` records the shell's first clock read. [Snapshot-restored sessions inherit startup values](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html), so neither this timestamp nor its lineage identifies a new VM start. Do not subtract it from a restored request to measure startup latency.

For optional detailed correlation, include `startupAttemptId` matching `^startup-[0-9a-f]{32}$`. `HTTP_HANDLER_ENTER` captures arrival at `POST /invocations`, before body reading/authentication; it proves neither readiness nor VM restoration.

Use JSON `wall_unix_ms` as producer time, not CloudWatch enqueue/ingestion time. Buffered records may use `delivery:"replay"` after the scoped logger initializes; later records use `"producer"`. Local replay does not guarantee delivery. Deduplicate producer/replay records and correlate attempt IDs with AgentCore Runtime version/image digest and `runtimeSessionId`: UUIDs may repeat across restored snapshots.

Measure intervals inside the handler. Client/server clocks can differ; monotonic values from different processes or hosts cannot be subtracted. Client round trips include networking. An unbound status response does not measure `USER_READY`, chat or channel E2E latency.

## Live acceptance gates

Live E2E uses real AWS resources, model/tools and channel messages. Use a dedicated V2 test identity and its saved route. The [runner](operations.md#run-tests) defaults to smoke/lifecycle tests and verifies the manifest even with `--skip-deploy`. `TestColdStart` requires a new identity; `TestFullStartup` checks bound full-OpenClaw completion without a latency floor.

Cover text/images, subagents, scoped files, API keys, skills, Browser/Gateway when enabled, Cron and channel delivery. Include restored state, long-idle resource recovery, failure/unknown outcomes and concurrent chat/Cron; check ownership isolation and absence of duplicate effects.

Record AgentCore Runtime version, image digest, model, state size and sample counts. Separate first-request, restored, warm and idle-recovery results. Offline checks and deployment warmup do not establish channel E2E or first-user latency; skipped checks remain unverified.
