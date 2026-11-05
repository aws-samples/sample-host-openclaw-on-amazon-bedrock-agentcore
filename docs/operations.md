# Operations

Operator commands for a deployed stack. Moved out of the [README](../README.md). AgentCore Runtime V2 uses the pinned SDK controller (`boto3` / `botocore` 1.43.95); see the [AgentCore Runtime V2 runbook](runtime-v2.md) for setup and verification. Deployment commands operate on live AWS resources.

## Check AgentCore Runtime status

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py verify \
  --region "$CDK_DEFAULT_REGION" --manifest .runtime/runtime-v2.json --require-enrollment-ready
```

The manifest records the deployment target. For a particular user, read `USER#<userId>` / `SESSION`: its `runtimeArn`, `endpointName`, `sessionId`, generation and canonical actor are authoritative even after a newer release is deployed. Do not substitute the current manifest for an existing route.

## Inspect sessions

Use the identity table's saved V2 route when inspecting or stopping a test session. `tests/e2e/session.py` resolves the route before stopping it and retains all identity/session records. Stop does not prove checkpoint completion or authorize migration. Missing or incomplete routes require reconciliation; there is no default AgentCore Runtime fallback or session-deletion reset.

Unknown invocation outcomes retain `activeAdmissions` and `inFlight`. Reconcile the exact invocation before changing state; do not clear ownership records or admission tokens to force a new registration.

## Deleting a user's data

`scripts/delete_user_data.py` erases one user: every linked channel, the session, schedules, files, secrets, the Cognito user, token-usage rows, container log streams, daily usage items and identity records. This is a deliberate end-of-life action, not a reset; to block a user without deleting anything, use `manage-allowlist.sh disable`. The tool is a dry run unless `--apply` is given:

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/delete_user_data.py \
  --actor telegram:123456789 --region "$CDK_DEFAULT_REGION"          # dry run: lists what it would delete
uv run --no-project --python .venv-deploy/bin/python python scripts/delete_user_data.py \
  --actor telegram:123456789 --region "$CDK_DEFAULT_REGION" --apply  # deletes
```

- **Who:** `--actor` takes any of the user's channel identities, and `--user-id user_<16 hex>` the internal user ID. Add `--env-suffix S`, or set `OPENCLAW_ENV_SUFFIX`, for a suffixed deployment. Table, bucket and pool names come from the stacks' CloudFormation outputs, and the schedule group from the suffix.
- **Order:**
  1. Revoke: `ALLOW#` records are deleted, and PROFILE gets `accessStatus = DISABLED`, as `disable` does.
  2. Stop: the route is frozen and the AgentCore session is stopped.
  3. Delete: schedules and `CRON#` records; every S3 object version and delete marker under `<namespace>/` and `_system/<namespace>/`, checked empty afterwards; `openclaw/user/<namespace>/*` and the Cognito password secret; the Cognito user; token-usage rows (`USER#<user ID>` and the older `USER#<runtime actor>` form); and `/openclaw/container` log streams (`<user ID>-*` and the older `<namespace>-*` form).
  4. Identity records go last. The step starts with the user's daily usage items and limit override (`USAGE#<user ID>`), which are not needed to find the user again.
- **Secrets:** they are deleted with a 7-day recovery window. `--force-delete-secrets` deletes them at once.
- **Refusals:** the tool refuses (exit 3) while the route holds invocation admissions, which means a turn is running or an unknown outcome needs reconciling (see Inspect sessions). It also refuses when the identity records disagree. When it refuses or a call fails (exit 1), re-run the same command: every step lists before it deletes, so a re-run finishes the job.
- **Ownership checks:** records outside the user's `USER#<id>` partition (`CHANNEL#`, `ACTOR#` and `NS#`) decide which actors and namespaces belong to the user. A `CRON#` record or back-reference in the partition that points elsewhere is reported and left alone.
- **Output:** the user appears by internal user ID only. A successful `--apply` ends with `AUDIT: user_<id> data deleted <date>`; keep that line.
- **Shared resources:** api-key secrets (`openclaw/user/<namespace>/*`) and the `/openclaw/container` log group are shared by every deployment in the account and region. Deleting the user in one deployment removes these copies for the others too.
- **Not deleted:** a Telegram group the user allowlisted stays allowlisted; its `addedBy` is reported. Also not deleted:
  - CloudTrail history;
  - Step Functions history of async scheduled runs;
  - events in shared log groups, until their retention ends;
  - ActorId metrics from before #230;
  - point-in-time recovery copies, for 35 days;
  - secrets inside their recovery window;
  - AgentCore session storage, until it expires;
  - mentions in other identities' data.

  The tool prints this list at the end of every run.
- **Re-registration:** with the ownership record gone, the same actor can register again (for example, when allowlisted). It gets the same internal user ID and an empty namespace.
- **Permissions:** the operator needs:
  - `cloudformation:DescribeStacks`;
  - DynamoDB `GetItem`, `Query`, `Scan`, `UpdateItem` and `DeleteItem` on the identity and token tables;
  - `s3:ListBucketVersions` and `s3:DeleteObjectVersion` on the user-files bucket;
  - `scheduler:ListSchedules`, `GetSchedule` and `DeleteSchedule`;
  - `secretsmanager:ListSecrets`, `DescribeSecret` and `DeleteSecret`;
  - `cognito-idp:AdminDeleteUser`;
  - `logs:DescribeLogStreams` and `DeleteLogStream`;
  - `bedrock-agentcore:StopRuntimeSession`.

## Namespace migration

A user's files, api-key secrets, schedules and Cognito username are named after the user's storage namespace. Until a user is migrated, that is the runtime actor with `:` replaced by `_` (for example `telegram_123456789`), so the channel user ID appears in S3 keys, secret and schedule names, and in the CloudTrail records of calls on them. The migration moves one user at a time to a random namespace, `ns_` followed by 26 base32 characters. The route stores it (`USER#<id>` / `SESSION.namespace`), and an owner record outside the user's partition, `NS#<namespace>` / `OWNER`, names the user it belongs to.

It has three stages. Each one can stop safely at its end:

1. **Dual-read release.** The Router, the Cron Lambda, the gateway tools and the bridge read the namespace from the route; a route without one uses the actor-derived namespace. Nothing moves. While a route is `MIGRATING`, the Router answers every message with "Maintenance in progress, please send that again in a minute." Deploy this release everywhere first: Phase 3 for the Lambdas, and a bridge image on a new endpoint (see [Deploy a new bridge version](#deploy-a-new-bridge-version)). A route keeps the endpoint it enrolled on ([runtime-v2.md](runtime-v2.md#existing-user-migration-and-cutover)), so only users enrolled on that endpoint or a later one can move in stage 3.
2. **Backfill.** `scripts/namespace_backfill.py` stores each route's current namespace and writes its `NS#` owner record. No data moves and no turn changes, because the stored value is the one every component derives today. This is safe for every user, whatever their endpoint.
3. **Cut-over.** `scripts/namespace_cutover.py` moves one user's data to a new namespace.

Both tools are dry runs unless `--apply` is given. Run the dry run first, and read what it reports:

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/namespace_backfill.py \
  --region "$CDK_DEFAULT_REGION"           # dry run
uv run --no-project --python .venv-deploy/bin/python python scripts/namespace_backfill.py \
  --region "$CDK_DEFAULT_REGION" --apply   # every route; --user-id or --limit N for fewer
uv run --no-project --python .venv-deploy/bin/python python scripts/namespace_cutover.py \
  --region "$CDK_DEFAULT_REGION" --user-id user_0123456789abcdef --endpoint release_3           # dry run
uv run --no-project --python .venv-deploy/bin/python python scripts/namespace_cutover.py \
  --region "$CDK_DEFAULT_REGION" --user-id user_0123456789abcdef --endpoint release_3 --apply
uv run --no-project --python .venv-deploy/bin/python python scripts/namespace_cutover.py \
  --region "$CDK_DEFAULT_REGION" --user-id user_0123456789abcdef --rollback --apply   # within the hold
uv run --no-project --python .venv-deploy/bin/python python scripts/namespace_cutover.py \
  --region "$CDK_DEFAULT_REGION" --user-id user_0123456789abcdef --cleanup --apply    # after the hold
```

`--endpoint` takes the name you gave the release with `--endpoint-name` (for example `release_3`).

- **Backfill results:** `stored` and `already stored` are done. `busy` means a turn was running or the route changed; re-run later (exit 1). `inactive` (the route is not `ACTIVE`), `unconfirmed` (no `ACTOR#` owner record confirms the route's actor) and `conflict` (the `NS#` record is missing or names another user) need reconciling first (exit 3).
- **Endpoints:** `--endpoint` names the endpoints whose bridge image reads the namespace from the route: the endpoint of the dual-read release and any later one. A user on another endpoint is refused, because an older bridge derives the namespace from the actor whatever the route stores.
- **Cut-over steps:**
  1. Plan: allocate the namespace and write its `NS#` owner record.
  2. Freeze: the route becomes `MIGRATING`, and the Router sends the maintenance reply.
  3. Stop: stop the AgentCore session, then wait until the old prefixes have not changed for `--quiesce-minutes` (default 10).
  4. Copy: the current version of every object under `<old>/` and `_system/<old>/`.
  5. Secrets: copy `openclaw/user/<old>/*`.
  6. Schedules: recreate each one under its new name and update `CRON#.ebName`.
  7. Flip: store the new namespace and bump the route's generation.
  8. Verify: copy again whatever changed under the old prefixes, then check the secrets and schedules.
  9. Unfreeze: the route becomes `ACTIVE` again.

  Progress is kept in `USER#<id>` / `NS_MIGRATION`, and the same command continues an interrupted run where it stopped (exit 1 means re-run). The tool refuses (exit 3) while the route holds invocation admissions, as `delete_user_data.py` does, and whenever the `NS#` and `ACTOR#` records disagree with the route or the migration record.
- **During the cut-over:** messages get the maintenance reply. A scheduled task that fires in that window fails once. The window is the quiet wait plus the copy time.
- **What is copied, and how:**
  - Objects: only current versions are copied; older versions stay under the old prefix until cleanup. Copies are new objects, so the user-files expiry (`user_files_ttl_days`) starts again for them. One copy call takes at most 5 GiB, so the plan refuses a user with a larger object.
  - Secrets: they keep their value, KMS key and tags; a tag whose value was the old namespace names the new one.
  - Schedules: they keep their expression and target input. A one-time schedule whose time has passed is moved disabled, so that it cannot run again.
  - Cognito: the user is not copied. The bridge creates the user under the new namespace at the next turn, and the old one stays until cleanup.
- **Rollback window:** the old data is kept for `--hold-days` (default 14) after the cut-over, and `--rollback` returns the user to it. Before the flip, it moves the schedules back and unfreezes. After the flip, it freezes and stops the route again, copies back the objects written under the new namespace since the cut-over and the secrets that differ, moves the schedules back, flips back and unfreezes. Either way it then deletes the new namespace's objects, secrets, Cognito user and `NS#` record. Files and secrets the user deleted after the cut-over come back.
- **Cleanup:** `--cleanup` is refused until the hold ends. Then it deletes:
  - every object version and delete marker under the old prefixes;
  - the old secrets and the old Cognito password secret, with a 7-day recovery window;
  - the old Cognito user;
  - `/openclaw/container` log streams named `<old namespace>-*`;
  - the old `NS#` record.

  It refuses while anything under the old prefixes was written after the cut-over was verified, since that means something still wrote there; compare those objects with the new namespace before deleting them by hand.
- **Not migrated:** the old namespace, or the actor it was made from, stays in:
  - CloudTrail event history (90 days; a dedicated or organization trail keeps its own copy);
  - Step Functions history of async scheduled runs (90 days after each run);
  - events in shared log groups, until their retention ends;
  - CloudWatch metrics with an ActorId dimension from before #230 (15 months);
  - point-in-time recovery copies of the identity table (35 days);
  - secrets inside their recovery window;
  - AgentCore session storage, until it expires;
  - transcripts and memory files that mention it. The skills refuse the old namespace with an error that points the agent to the user ID in its system prompt.

  Schedule inputs keep the actor ID, as they do today.
- **Deleting a migrated user:** `scripts/delete_user_data.py` finds both namespaces while a migration or its hold is in progress, and the remaining one afterwards.
- **Output:** users are named by internal user ID only.
- **Permissions:** the cut-over needs:
  - DynamoDB `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` and `Query` on the identity table (the backfill also needs `Scan`);
  - `s3:ListBucket`, `GetObject`, `PutObject`, `ListBucketVersions` and `DeleteObjectVersion` on the user-files bucket, with `kms:Decrypt` and `kms:GenerateDataKey` on its key;
  - `scheduler:ListSchedules`, `GetSchedule`, `CreateSchedule`, `UpdateSchedule` and `DeleteSchedule`, with `iam:PassRole` for the scheduler role;
  - `secretsmanager:ListSecrets`, `DescribeSecret`, `GetSecretValue`, `CreateSecret`, `PutSecretValue`, `TagResource` and `DeleteSecret`;
  - `cognito-idp:AdminDeleteUser`;
  - `logs:DescribeLogStreams` and `DeleteLogStream`;
  - `bedrock-agentcore:StopRuntimeSession`;
  - `cloudformation:DescribeStacks`.

## Deploy a new bridge version

Install the `uv` environments described in [Quick Start](../README.md#quick-start). Set `image_version` or supply a reviewed ARM64 image. Preserve the previous release manifest, choose an unused endpoint name, then run:

```bash
./scripts/deploy.sh --runtime-only --endpoint-name release_2
./scripts/deploy.sh --phase3
```

Add `--image-uri "$IMAGE_URI"` to use a prebuilt image. The default manifest is `.runtime/runtime-v2.json`, passed to CDK as `runtime_manifest`. Initial deployment uses `release_1`; subsequent changed releases require new immutable endpoint names. Existing users retain their saved endpoint/version. A CDK-only deployment does not update the AgentCore Runtime image.

**Deploy order for the user ID token attribution change (bridge image that sends `openclaw.user_id`): deploy the token-metrics Lambda first, then the bridge image.** The new image names the user in Bedrock request metadata by internal user ID and no longer sends `openclaw.actor_id`. A token-metrics Lambda from before this change ignores such records, so their usage is attributed to `default-user` until the Lambda is updated. On an existing deployment, run Phase 3 against the current manifest first; it updates the token-metrics Lambda (and the Router), and the new Lambda still reads records from older images. Then deploy the image and run Phase 3 again as usual:

```bash
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json   # 1. token-metrics Lambda (and Router) on the current release
./scripts/deploy.sh --runtime-only --endpoint-name release_2       # 2. the new bridge image
./scripts/deploy.sh --phase3                                       # 3. as usual
```

A fresh deployment (`./scripts/deploy.sh` without a mode) needs no extra step: the token-metrics Lambda and its log subscription are created in Phase 3, so only the new Lambda ever processes records. After the change, new token-usage rows are keyed `USER#<user ID>` and new container log streams are named `<user ID>-<epoch ms>`; rows and streams written before keep the actor and namespace forms until they expire, and `scripts/delete_user_data.py` deletes both forms.

## Update shared stacks

```bash
./scripts/deploy.sh --phase1
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json
```

Phase 1 needs no manifest. Phase 3 requires successful warmup and live V2 readback; it grants invocation permissions and configures new registrations. Preserve older endpoints and dependencies used by existing routes. Platform selectors, V1 maintenance, fallback and enrollment opt-in flags are removed.

## Turn on the per-user daily cost limit

Set `per_user_daily_cost_usd` in `cdk.json` (USD; see the [README](../README.md#per-user-daily-cost-limit)) and run Phase 3:

```bash
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json
```

- **No deploy order is needed.** Phase 3 deploys the token-metrics Lambda, which writes the daily usage items (`USAGE#<user ID>` / `DAY#<day>` in the identity table), and the Router and Cron Lambdas, which read them. A user without an item for today has no usage, so nothing is refused before the new token-metrics Lambda runs, and its items change nothing while nobody reads them. No bridge image change is involved.
- **The first day is counted from the upgrade.** The items start with the first call the new token-metrics Lambda processes, so on the day it is deployed earlier calls are not in the count.
- **Turning it off:** set it to `0` and run Phase 3 again. The Router and Cron then read no usage before a turn; `/usage` keeps working, and the items keep being written.
- **Overrides and inspection:** `scripts/manage-allowlist.sh set-cap`, `clear-cap` and `usage`. They need DynamoDB `GetItem`, `PutItem`, `DeleteItem` and `Query` on the identity table.
- **Alarm:** `openclaw-token-metrics-errors` fires when the token-metrics Lambda fails or logs a record or usage item it could not write (`Failed to process log event`, `Usage aggregate not written`) within an hour. That usage is missing from the count.

## Run tests

Install the pinned SDK, pytest and the bridge dependencies using the [AgentCore Runtime V2 offline setup](runtime-v2.md#offline-checks). Those checks use fake clients/servers and synthetic CDK context. `scripts/run-unit-tests.sh` runs every unit-test suite from the repository root without AWS credentials; it never runs E2E tests. The [Unit tests workflow](../.github/workflows/unit-tests.yml) runs the same script on every pull request and on pushes to `main` and `release/v1`.

```bash
scripts/run-unit-tests.sh                  # all suites: bridge, skills, gateway-tools, router, cron, tests (tests/ without tests/e2e)
scripts/run-unit-tests.sh router cron      # selected suites; `node` and `python` select each group, --help lists them
```

To run one suite or file, run each command below independently from the repository root. Keep Router and Cron tests in separate Python processes because both use a module named `index`. Channel E2E uses real AWS resources, model/tool calls and channel messages; use a dedicated V2 test identity and its saved route.

```bash
cd bridge && node --test *.test.js                     # bridge unit tests (Node 24)
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test subagent-routing.test.js      # subagent model routing + detection tests
cd bridge && node --test content-extraction.test.js    # recursive content block extraction tests
cd bridge && node --test scoped-credentials.test.js    # per-user STS credential scoping tests
cd bridge && node --test workspace-sync.test.js        # workspace sync + SQLite snapshot tests
cd bridge && node --test state-storage.test.js         # local state dir / session-storage mirror + restore tests
cd bridge && node --test read-body.test.js             # UTF-8 request body decoding + size cap (8 tests)
cd bridge && node --test openclaw-tool-deny.test.js    # channel-delivery tools denied in openclaw.json (7 tests)
cd bridge/skills/api-keys && node --test migrate.test.js # api-keys migrate.js/native.js key-loss tests (9 tests)
cd bridge && node --test gateway-mcp.test.js           # Gateway MCP config and Cognito token provider (8 tests)
cd bridge && node --test runtime-skills.test.js        # runtime skill manifest + cold-start reinstall (48 tests)
node --test lambda/gateway_tools/*.test.js             # Gateway tool Lambdas: JWT verification, namespace scoping, interceptor (31 tests, Node 24)
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router -q
PYTHONPATH=lambda/shared/python:lambda/cron uv run --no-project --python .venv/bin/python python -m pytest lambda/cron -q
uv run --no-project --python .venv/bin/python python -m pytest tests/test_agentcore_exec.py -v                     # operator CLI tests (mocked boto3)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_delete_user_data.py -v                  # user data deletion tool (stubbed AWS clients)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_guardrail_eval.py -v                     # guardrail eval script tests (stubbed ApplyGuardrail)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_state_bucket_lifecycle_synth.py -v       # user-files bucket lifecycle rules (synth, no AWS)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_gateway_stack_synth.py -v                # OpenClawGateway synth: flag off leaves the 8 templates unchanged, flag on adds the stack (10 tests, no AWS)

# E2E tests (requires deployed stack + E2E_TELEGRAM_CHAT_ID/E2E_TELEGRAM_USER_ID env vars)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k smoke               # connectivity + webhook auth
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k lifecycle            # full message lifecycle
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k cold_start           # first registration; skips existing identities
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k full_startup          # V2 USER_READY + full OpenClaw completion
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k ScopedCredentials     # S3 file write/read/delete via scoped creds
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k conversation          # multi-turn + rapid-fire
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k SkillManagement       # clawhub skill install/uninstall/list
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k ApiKeyManagement      # API key storage (native + Secrets Manager)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k CronSchedule          # cron lifecycle + CRON# DynamoDB record check
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k GuardrailSecurity     # guardrail content filtering (requires BEDROCK_GUARDRAIL_ID env var, see below)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/test_guardrail_wiring.py -v             # guardrail WIRING: passes only on a logged guardrail intervention
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/test_gateway_tools.py -v -m gateway     # Gateway MCP tools (4 tests; needs enable_gateway=true, skipped when OpenClawGateway is not deployed)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v                          # all E2E tests
```

`scripts/e2e-deploy-and-test.sh` defaults to `tests/e2e/bot_test.py::TestSmoke` and `tests/e2e/bot_test.py::TestMessageLifecycle`. Repeating `--test-node` replaces that selection. `--skip-deploy` still requires manifest enrollment verification. The runner never deletes SESSION records, stops AgentCore Runtime sessions or rewrites `image_version`.

When `E2E_TELEGRAM_CHAT_ID` is set, the runner attempts a Telegram start notice before deployment/verification and a completion notice after pytest finishes. Notification failures do not change the test result. Deployment or verification failures exit before the completion notice. Pytest output is saved to `${E2E_RESULTS_FILE:-/tmp/e2e-results.txt}` and summarized from the log; the runner returns pytest's exit code. Consult the full output for authoritative test results.

```bash
# Live E2E: use a dedicated V2 test identity and its saved route.
./scripts/e2e-deploy-and-test.sh --skip-deploy --manifest .runtime/runtime-v2.json
./scripts/e2e-deploy-and-test.sh --skip-deploy \
  --test-node tests/e2e/bot_test.py::TestFullStartup \
  --test-node tests/e2e/bot_test.py::TestConversation
```

Selected tests retain their own model/tool side effects, in addition to the runner's Telegram notices. A saved user's route can refer to an older release than the manifest; record both when attributing evidence. Skipped or unexecuted suites do not count as release evidence.

## Security validation

```bash
cdk synth   # Runs cdk-nag AwsSolutions checks — should produce no errors
```
