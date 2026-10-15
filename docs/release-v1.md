# The `release/v1` branch (AgentCore Runtime V1)

`main` deploys OpenClaw on AgentCore Runtime **V2** (snapshot restore, see the [AgentCore Runtime V2 runbook](runtime-v2.md)). V2 is only available in some AWS Regions. The `release/v1` branch keeps the **V1** deployment for every other Region where AgentCore Runtime is available, and for existing V1 deployments.

## Which branch to use

| | `main` (V2) | `release/v1` (V1) |
|---|---|---|
| AgentCore Runtime platform | V2 (`platformVersion="V2"`) | V1 (the AgentCore default) |
| Regions | `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `ap-northeast-1` ([AWS list](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions); `V2_REGIONS` in `scripts/runtime_deploy.py` rejects any other Region) | Any Region with AgentCore Runtime |
| Runtime deployment | Pinned SDK controller (`scripts/runtime_deploy.py`, `.venv-deploy`), immutable `release_N` endpoints, runtime manifest `.runtime/runtime-v2.json` | AgentCore Starter Toolkit (`agentcore` CLI), runtime `openclaw_agent`, generated `.bedrock_agentcore.yaml` |
| First reply in a new session | Waits for state restore and full OpenClaw startup; no warm-up agent | A lightweight warm-up agent (`bridge/lightweight-agent.js`) answers while OpenClaw starts |
| Default primary model | `global.anthropic.claude-haiku-5-5`, fallback `global.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-sonnet-4-6` |
| New features | Yes | Fixes only (backports) |

Pick `main` if your Region supports V2 and you are starting a new deployment. Pick `release/v1` if your Region does not support V2, or if you already run a V1 deployment and want its fixes.

## Existing V1 deployments

There is no in-place migration from V1 to V2. The V2 controller creates a separate AgentCore Runtime (`openclaw_agent_v2`); it does not convert the V1 runtime, and existing-user migration and cutover are not implemented (see [Existing-user migration and cutover](runtime-v2.md#existing-user-migration-and-cutover)). Users already registered on a V1 deployment keep their V1 route.

Both branches use the same CloudFormation stack names (`OpenClawVpc`, `OpenClawSecurity`, `OpenClawAgentCore`, `OpenClawRouter`, `OpenClawCron`, `OpenClawTokenMonitoring` and the others), so do not deploy `main` over a V1 deployment in the same account and Region, and do not run both branches side by side there. If you run V1, stay on `release/v1`.

## What `release/v1` contains

`release/v1` starts from `main` as it was just before the V2 migration (PR #141, which follows #140), plus these backports:

| `release/v1` PR | Backport of | Change |
|---|---|---|
| #153 | #146 | Proxy sends history tool blocks as text when a request has no tools, so a tool-less request after a tool turn is no longer rejected by Bedrock |
| #154 | #152 | DynamoDB deletion protection and the `table_tags` context key |
| #155 | #147, #150 | Cron: no raw invocation errors to users, no Lambda async retry, `callerBudgetMs`; brief audio through the `BRIEF_AUDIO` marker and Telegram `sendAudio` |
| #156 | #149, #147 | Stale sub-agent delivery sweep, final-answer-only delivery, clean cron failures at the caller's deadline |
| #159 | #157 | Router logs only the exception type when a secret or token fetch fails |
| #160 | #158 | Token metrics: nested invocation-log token counts, Opus 4.6 price, prompt-cache tokens, unknown-model warning |

The behaviour of these fixes is described in the `main` docs: [scheduled tasks](how-it-works.md#scheduled-tasks-cron-jobs), [brief audio](how-it-works.md#brief-audio), [token usage tracking](how-it-works.md#token-usage-tracking) and [implementation notes](how-it-works.md#implementation-notes).

## Deploy `release/v1`

```bash
git clone https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore.git
cd sample-host-openclaw-on-amazon-bedrock-agentcore
git checkout release/v1
```

Then follow the README **on that branch**. In short: install the AgentCore Starter Toolkit (`pip install bedrock-agentcore-starter-toolkit`, with `agentcore` on your `PATH`), then run `./scripts/deploy.sh`. Its three phases are CDK foundation stacks, the Starter Toolkit runtime deployment, and the CDK dependent stacks. That README also covers `BUILD_MODE=codebuild`, `--phase1` / `--runtime-only` / `--phase3` / `--cdk-only`, channel setup and cleanup for V1.

To update an existing V1 deployment, pull `release/v1` and run `./scripts/deploy.sh` again. The `#152` backport adds deletion protection to both DynamoDB tables; see the `table_tags` row in the [Configuration table](../README.md#configuration) if your account runs automated cleanup of untagged resources.

## V1 and V2 compared

What the platform and the code change, as documented:

- **Startup.** V2 prepares a user-neutral OpenClaw gateway once and restores that snapshot for each new session, so new users start from a consistent prepared state ([AWS: platform versions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions)). V1 starts the container per session and covers the startup with the lightweight warm-up agent.
- **No warm-up shim on V2.** On V2 the first message of a session waits for `USER_READY`: state restore, scoped credentials, gateway and Browser readiness. For a user with saved state, the first reply after a cold start therefore comes later than V1's warm-up reply, which answers before OpenClaw is ready but with a reduced tool set.
- **Warm sessions.** Once OpenClaw is running, both branches run OpenClaw `2026.9.5` behind the same Bedrock proxy, so warm replies are expected to be similar. Reply time then depends mostly on the model, and the default primary models differ (see the table above).
- **Storage.** Both keep the OpenClaw state dir on local disk, mirrored to per-session storage and backed up to S3 ([state storage](session-storage.md)).
- **Deploy time.** A V2 create or update prepares and snapshots the environment and takes several minutes to reach `READY`; V1 reaches `READY` in seconds (AWS docs above).

<!-- TODO: add cold-start and warm-reply timings for V1 and V2 from the running comparison, labelled "measured on a test deployment" with the date, model, state size and sample counts. Do not add numbers before that comparison finishes. -->
