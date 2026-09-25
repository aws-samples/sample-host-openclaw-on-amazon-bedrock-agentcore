# AWS Agent Registry pilot: approval gate and shared catalogue for Gateway MCP tools

Status: **plan only, nothing implemented.** Rating from the research this plan follows: *pilot,
narrowly*. No code, stack or flag described here exists yet; every "would" below is a proposal.

The question that prompted it: can several OpenClaw deployments share one approved set of tools
without an image rebuild, with someone other than the deployer approving each addition? For tools
served by the [AgentCore Gateway](gateway-mcp-tools.md) the answer is yes, and AWS Agent Registry
is the natural place for the approval boundary. For OpenClaw *skills* the answer is no, and this
plan deliberately leaves them out.

## 1. Scope

**In scope.** Registry as the source of truth for *which Gateway-hosted MCP servers* the bridge
may hand to OpenClaw, and as the approval gate in front of that list. One record per Gateway (or
per MCP server behind a Gateway), `recordType = MCP`. Consumers (the contract server in each
user's microVM) see only approved records.

**Out of scope, on purpose.** Registry is **not** a skill-delivery mechanism. A `SKILL` record
holds a `SKILL.md` used "only as metadata for discovery purpose"; the service "does not support
storing other agent skill files"
([registry-supported-record-types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-supported-record-types.html)).
Skill code would still have to be installed from ClawHub or Git into `/skills`, which is not
persisted across microVMs today (`bridge/state-storage.js`, `bridge/workspace-sync.js` mirror
`~/.openclaw` only). Fixing skill persistence is a separate piece of work on a separate branch and
is a prerequisite for *any* runtime skill story, Registry or not. This pilot does not touch
`bridge/skills/`, the Dockerfile skill list, or `skills.load.extraDirs`.

**When to move from pilot to adoption.** Any one of:

* two or more OpenClaw deployments (accounts or regions) must expose the same approved tool set;
* an audit requirement that tool additions be approved by someone other than the person who
  deploys the stacks;
* `enable_gateway` becomes the default.

**When to stop.** The Gateway stays opt-in and there is a single deployment; or the pilot shows the
post-ready catalogue refresh (section 3) changes the tool set mid-session in a way the model
handles badly. With one deployment and the Gateway off, the whole "catalogue" is one URL and
Registry adds a network dependency, an IAM surface and a stack for no consumer.

## 2. Architecture

### Service facts the design relies on

| Fact | Source |
|---|---|
| Name: **AWS Agent Registry** (formerly AgentCore Registry). Dedicated `agent-registry` namespace GA **2026-08-06**; the preview `bedrock-agentcore` registry namespace **shuts down 2026-10-30** and is closed to new customers. Endpoints `agent-registry.<region>.api.aws` (data) and `agent-registry-control.<region>.api.aws` (control); IAM prefix `agent-registry:` | [registry-faq (migration guide)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html), [registry-iam-permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-iam-permissions.html) |
| Available in us-east-1, us-west-2, eu-west-1, ap-southeast-2, ap-northeast-1 (both regions this project deploys to). Re-check before deploying; this table changes | [agentcore-regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) |
| A registry holds records; `recordType` in {AGENT, MCP, SKILL, CUSTOM}; an MCP record is an official MCP `server.json` plus a `tools` array. Records are **metadata**: the Registry's own MCP endpoint only exposes `search_/list_/batch_get_discoverable_registry_records` and never proxies tool calls | [registry-concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-concepts.html), [registry-mcp-endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-mcp-endpoint.html) |
| Lifecycle: DRAFT -> PENDING_APPROVAL (EventBridge event on the default bus) -> APPROVED or REJECTED via `UpdateRegistryRecordStatus`; auto-approval optional per registry; consumers see only approved revisions. Editing an approved record creates a DRAFT revision while the approved one stays discoverable. Deprecation is terminal; rejecting hides a record | [registry-record-lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-record-lifecycle.html), [registry-key-capabilities](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-key-capabilities.html) |
| `name` + `recordVersion` is the uniqueness key, so several versions of one tool server coexist as separate records | [registry-record-lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-record-lifecycle.html) |
| Inbound auth for discovery and the MCP endpoint: IAM/SigV4 **or** JWT (OIDC discovery URL plus allowed audiences / clients / scopes / custom-claim rules). Auth type and discovery URL are immutable after creation. Control plane is always IAM and CloudTrail-logged | [registry-supported-auth-types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-supported-auth-types.html) |
| Cross-account: RAM sharing with managed permissions (ReadOnly default, Consumer, Publisher, Administrator); org-internal shares need no invitation; `UpdateRegistry`/`DeleteRegistry` are never delegable | [registry-cross-account-sharing](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-cross-account-sharing.html) |
| Record synchronization: a record can point at a live MCP server URL and Registry pulls its name, description and tool list into a new revision using an outbound credential provider (OAuth or IAM) | [registry-key-capabilities](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-key-capabilities.html) |
| CloudFormation: `AWS::AgentRegistry::Registry` (`Name`, `AuthorizerType` CUSTOM_JWT or AWS_IAM, `DiscoveryConfiguration`, `ApprovalConfiguration`) and `AWS::AgentRegistry::RegistryRecord` (`RegistryId`, `Name`, `RecordType` MCP / AGENT / SKILL / CUSTOM / GATEWAY, `RecordVersion`, `Descriptors`). The record resource has **no status property**: CloudFormation creates the record, approval stays an API call | [AWS::AgentRegistry::Registry](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-agentregistry-registry.html), [AWS::AgentRegistry::RegistryRecord](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-agentregistry-registryrecord.html) |
| Pricing: first 5,000 net records/month free, then $0.40 per 1,000; first 1,000,000 searches/month free, then $0.02 per 1,000; first 2,000,000 list+get/month free | [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) |

### Records

One registry per hub account and region, `openclaw-tools-<stage>`, JWT inbound auth against the
existing `OpenClawSecurity` Cognito user pool (`discovery_url` = the pool's
`/.well-known/openid-configuration`, `allowed_clients = [openclaw-proxy client id]`), exactly the
wiring `stacks/gateway_stack.py` uses for the Gateway's `CUSTOM_JWT` authorizer. Manual approval
(auto-approval off).

Records, all `recordType = MCP`:

| Record `name` | `recordVersion` | Descriptor | Who creates it |
|---|---|---|---|
| `openclaw-gateway` | image/stack version, e.g. `1` | `server.json` with the Gateway URL (`GatewayUrl` output), the Gateway ARN in a `_meta`/custom field, and the `tools` array copied from `lambda/gateway_tools/tool-schemas.json` (today: `list_files`, `read_file`, `write_file`, `delete_file`, `create_schedule`, `list_schedules`, `update_schedule`, `delete_schedule`) | The `OpenClawRegistry` stack of the deployment that owns the Gateway |
| `openclaw-gateway` | `2`, `3`, ... | Same shape, produced when the tool list or Gateway changes | Same stack on the next deploy, or a publisher in another deployment |
| `<other-deployment>-gateway` | any | Another deployment's Gateway, submitted through RAM "Publisher" | That deployment's stack |

Records point at the Gateway; the Gateway stays the thing that serves tools and enforces per-user
identity (interceptor + Lambda JWT re-verification, unchanged from [gateway-mcp-tools.md](gateway-mcp-tools.md#identity-where-the-namespace-comes-from)).

### Approval states and roles

```
publisher (deploy role / another account via RAM Publisher)
   CreateRegistryRecord  ->  DRAFT
   SubmitRegistryRecord  ->  PENDING_APPROVAL   -- EventBridge event -> notification (Slack/ticket)
curator (separate IAM role, a human)
   UpdateRegistryRecordStatus APPROVED  ->  discoverable to consumers
   UpdateRegistryRecordStatus REJECTED  ->  hidden
consumer (each user's contract server, with the user's own Cognito access token)
   ListDiscoverable / BatchGetDiscoverable / InvokeRegistryMcp  ->  approved records only
```

* **Publish vs approve are different principals.** The deploy role gets create/update/submit
  only; a curator role gets `UpdateRegistryRecordStatus` only; neither gets
  `AgentRegistryFullAccess`. This separation is the reason to run the pilot at all.
* **Rollback** = approve the older `recordVersion` record again (or leave it approved) and reject
  the newer one. There is no revert call; deprecation is terminal, so a record that may need to
  come back is *rejected*, not deprecated
  ([registry-record-lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-record-lifecycle.html)).
* **Record synchronization** (Registry pulling `tools/list` from the Gateway) is *not* used in
  the pilot: the Gateway's `CUSTOM_JWT` authorizer needs an OAuth credential provider on the
  Registry side, and each sync creates a revision that a curator could rubber-stamp. The pilot
  copies the tool list from `tool-schemas.json` at deploy time, so the reviewed JSON in the repo
  is what the curator approves. Revisit once question 5 in section 8 is answered.

## 3. Bridge changes (proposal)

Principle: **the boot path does not change.** The Registry is consulted only after OpenClaw is
ready, and a failure to reach it leaves behaviour identical to today.

| Step | Where | Change |
|---|---|---|
| Boot | `bridge/agentcore-contract.js` `setupGatewayBearer()` (around line 1262) and `writeOpenClawConfig()` | Unchanged. The static `mcp.servers.agentcore` entry from `AGENTCORE_GATEWAY_URL` is written exactly as today, so the first tool call after a cold start needs no Registry round trip |
| After ready | `pollOpenClawReadiness()` (line 924), once `openclawReady = true` | New `refreshRegistryCatalog(actorId)`: `POST` `tools/call list_discoverable_registry_records` (filter `recordType = MCP`) to `https://agent-registry.<region>.api.aws/registry/<registryId>/mcp` with `Authorization: Bearer <the same Cognito access token the Gateway bearer uses>`; `batch_get` the results; build `mcp.servers.<recordName>` entries; diff against the current `openclaw.json`; rewrite atomically (tmp + rename, the `rewriteBearer` pattern). OpenClaw hot-applies `mcp` changes and retires only the changed server connections (`docs/gateway/configuration/hot-reload.md` in OpenClaw 2026.9.5, cited in [gateway-mcp-tools.md](gateway-mcp-tools.md#bridge-behaviour)) |
| New module | `bridge/registry-catalog.js` (pure functions + one fetch) | `fetchApprovedMcpRecords(registryMcpUrl, token)`; `toMcpServers(records, token, { allowedGatewayHosts })` returns entries only for allow-listed hosts (section 4) with `toolFilter.include` set to the record's `tools[].name`. Unit tests in `bridge/registry-catalog.test.js` |
| Generalise | `bridge/gateway-mcp.js` `rewriteBearer()` | Loop over every server whose entry carries our bearer, not only `agentcore`, so the 5-minute refresh keeps every Registry-derived entry valid |
| Cache | `/mnt/workspace/.openclaw/registry-cache.json` (already inside the mirrored state dir) | Written on every successful refresh with a `fetchedAt`. Read at boot **only** to decide whether a second entry existed last time; still never on the critical path. TTL 1 h (estimate; tune in the pilot) |
| Fallback | everywhere | Registry unreachable, 4xx, malformed response, or no approved record: log `[registry] catalog unavailable (<reason>); keeping static mcp.servers.agentcore` and do nothing. Fail open to the static entry, never to an empty tool set |
| Env | `AGENTCORE_REGISTRY_MCP_URL` (from the `RegistryMcpUrl` output) | Set only when `enable_registry` is on; absent means every code path above is skipped, the same rule `AGENTCORE_GATEWAY_URL` follows (`bridge/gateway-mcp.js` `isEnabled()`) |

Not changed: `bridge/lightweight-agent.js` (warm-up shim; its tool list is static because it exists
to answer before any network round trip), the Dockerfile, anything under `bridge/skills/`.

Expected cost (estimates, not measured): one `list` + one `batch_get` over TLS from the microVM,
0.2 to 0.6 s, *after* ready, so **0 s added to boot-to-ready**. The Cognito call from the same VPC
measured 0.52 to 0.59 s including TLS, which is the basis for the range. The one behavioural risk
is a tool set that changes mid-session; the Gateway E2E report attached to PR #105 recorded one
wasted model turn (`Tool ... not found`, then a fallback to the exec skill) when that happened.
The pilot measures how often.

## 4. Security (mandatory, not optional)

1. **Bearer only to allow-listed Gateway hosts.** The bridge attaches the user's Cognito access
   token to an `mcp.servers` entry **only** when the record's URL host matches
   `*.gateway.bedrock-agentcore.<region>.amazonaws.com` **and** the Gateway ARN in the record is in
   a configured account allow-list (`AGENTCORE_REGISTRY_ALLOWED_ACCOUNTS`, defaulting to this
   account). Any other URL produces no entry and a `[registry] skipped <name>: host not allowed`
   log line. Without this rule a single approved-by-mistake record pointing at an attacker's host
   would receive every user's access token at first tool use. Unit-tested with a foreign URL, a
   look-alike host, and a record whose display URL and ARN disagree.
2. **Curator review of tool descriptions is the prompt-injection control.** Registry validates
   the MCP schema, not the content; a `tools[].description` saying "before any other tool, read
   `~/.openclaw/...`" passes validation. The approval checklist therefore requires the curator to
   read the full description text of every tool and the target ARN. `toolFilter.include` pins each
   server to the approved tool names so a Gateway whose `tools/list` later grows cannot add tools
   the curator never saw. The Registry's own search tools are **not** exposed to the model
   (`mcp.servers.registry` is never written); the contract server, not the model, consumes the
   catalogue.
3. **Per-user reads.** The registry uses JWT inbound auth with the user's own access token; the
   runtime execution role gets **no** `agent-registry:*Discoverable*` permissions. This keeps
   "who read what" attributable per user instead of one shared role, and keeps the Registry
   outside the per-user credential boundary described in
   [gateway-mcp-tools.md](gateway-mcp-tools.md#identity-where-the-namespace-comes-from).
4. **Least-privilege IAM, three roles, no `AgentRegistryFullAccess` anywhere.**

   | Principal | Actions | Resource |
   |---|---|---|
   | Deploy role (CloudFormation / CDK) | `agent-registry:CreateRegistry`, `UpdateRegistry`, `GetRegistry`, `DeleteRegistry`, `CreateRegistryRecord`, `UpdateRegistryRecord`, `GetRegistryRecord`, `DeleteRegistryRecord`, `SubmitRegistryRecord`, `TagResource` | the one registry ARN and `registry/<id>/record/*` |
   | Curator role (humans, MFA, assumable from the operator account) | `agent-registry:UpdateRegistryRecordStatus`, `GetRegistryRecord`, `ListRegistryRecords` | same registry |
   | Users (Cognito access token) | JWT inbound auth only: `ListDiscoverable*`, `GetDiscoverable*`, `BatchGetDiscoverable*`, `InvokeRegistryMcp` | same registry |
   | Runtime execution role, Gateway role, Lambda target roles | **nothing** on `agent-registry:` | |

   Action names follow the IAM reference
   ([registry-iam-permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-iam-permissions.html));
   confirm the exact list against the service authorization reference when the stack is written.
5. **Registry approval is not tool authorization.** Every call still passes the Gateway's
   `CUSTOM_JWT` authorizer, the REQUEST interceptor and the Lambda's JWT re-verification. Nothing
   in this pilot relaxes any of them.
6. **Notification on every submission.** EventBridge rule on the Registry's
   PENDING_APPROVAL event -> SNS/Slack, so a submission cannot sit unnoticed and a curator sees the
   diff before approving. The awslabs `admin-approval-workflow` sample has the EventBridge wiring
   ([samples: 03-registry](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-features/07-centralize-and-govern-your-ai-infrastructure/03-registry)).

## 5. Infrastructure as code

* **Flag:** `enable_registry` in `cdk.json`, default `false`, valid only together with
  `enable_gateway: true` (`app.py` raises if `enable_registry` is true and `enable_gateway` is
  not). Mirrors the `enable_gateway` pattern: `app.py` instantiates `RegistryStack` only when the
  flag is on, so with the default every existing template synthesizes byte-identical, checked the
  same way `tests/test_gateway_stack_synth.py` checks the Gateway (two hermetic synths diffed
  file by file).
* **Stack `OpenClawRegistry`** (`stacks/registry_stack.py`):
  * `AWS::AgentRegistry::Registry` `openclaw-tools-<stage>`: `AuthorizerType = CUSTOM_JWT`,
    `DiscoveryConfiguration` with the Cognito discovery URL and `allowed_clients = [proxy client
    id]` (same inputs `GatewayStack` receives from `security_stack`), `ApprovalConfiguration` =
    manual.
  * `AWS::AgentRegistry::RegistryRecord` `openclaw-gateway`, `RecordType = MCP`,
    `RecordVersion` = the `image_version` context value, `Descriptors.mcpServer` built at synth
    time from `GatewayUrl` and `lambda/gateway_tools/tool-schemas.json`.
  * Curator IAM role and the EventBridge -> SNS notification rule from section 4.
  * Outputs `RegistryId`, `RegistryArn`, `RegistryMcpUrl`
    (`https://agent-registry.<region>.api.aws/registry/<RegistryId>/mcp`).
  * cdk-nag `AwsSolutions` in tests, as for the Gateway stack.
* **CloudFormation support: yes.** Both resource types exist in the template reference (see the
  table in section 2), so no custom resource is needed for the registry or the record. Two
  things CloudFormation does *not* do and the plan does not want it to: approve records (that is
  the curator's API call, by design) and RAM-share the registry to other accounts (a later
  `AWS::RAM::ResourceShare` once a second deployment exists).
* **CDK binding.** `requirements.txt` pins `aws-cdk-lib>=2.170.0,<3.0.0`. Whether the installed
  version ships the `aws_agentregistry` L1 module (`CfnRegistry`, `CfnRegistryRecord`) was not
  verified on this host. Fallback that works on any version: `aws_cdk.CfnResource` with
  `type="AWS::AgentRegistry::Registry"` and the property dictionary from the template reference.
  The `deploy.sh` SDK-step fallback from the research is therefore not needed.
* **Deploy:** `scripts/deploy.sh` reads `RegistryMcpUrl` with `require_cdk_output` when the flag
  is on (the `GatewayUrl` block at lines 225 to 241 is the template) and passes
  `AGENTCORE_REGISTRY_MCP_URL` and `AGENTCORE_REGISTRY_ALLOWED_ACCOUNTS` to the runtime. First
  deploy leaves the record in DRAFT/PENDING_APPROVAL; the runbook step "curator approves
  `openclaw-gateway` v`<image_version>`" is a documented manual action, not something the deploy
  script does.

## 6. Pilot test plan (us-west-2 staging, `enable_gateway: true`, `enable_registry: true`)

Same harness as the Gateway E2E (`tests/e2e/`, 10 cold-start iterations, session record deleted
and runtime session stopped before each). Baseline to beat: Gateway-on **boot -> OpenClaw ready
p50 10.59 s / p95 10.80 s** ([gateway-mcp-tools.md](gateway-mcp-tools.md#performance-us-west-2-staging-2026-09-24)).

| # | Test | Pass condition |
|---|---|---|
| T1 | Boot latency | Boot -> ready p50 within **+0.5 s** of 10.59 s (i.e. <= 11.1 s) and the `[registry] catalog applied` line appears **after** `openclawReady=true` in every run. Also record the lookup itself (log `[registry] lookup <ms>`) to replace the 0.2 to 0.6 s estimate with a measurement |
| T2 | Cognito access token accepted | `POST <RegistryMcpUrl>` `initialize` + `tools/call list_discoverable_registry_records` with the user's **access** token -> 200; the same user's **ID** token -> 401/403 (the Gateway refused the ID token with `403 insufficient_scope`; confirm the Registry behaves the same). Run from a container via `scripts/agentcore-exec.py` |
| T3 | Approve end to end | Record `openclaw-gateway` PENDING_APPROVAL -> curator approves -> next cold start writes `mcp.servers.openclaw-gateway`; `tool=openclaw-gateway__...` appears in the container log for a file prompt; **no CodeBuild run** in between |
| T4 | Reject | Curator rejects -> next cold start: no `mcp.servers.openclaw-gateway`, static `agentcore` still works, `tests/e2e/test_gateway_tools.py -m gateway` still 4/4 |
| T5 | Rollback | Publish v2 with one extra (dummy) tool, approve v2, reject v1 -> tool visible; re-approve v1, reject v2 -> the extra tool is gone on the next cold start and never called in between (assert absence of `tool=...dummy` in logs) |
| T6 | Unapproved never visible | New record left in DRAFT and another in PENDING_APPROVAL for the whole run: across 10 cold starts and a prompt that names the dummy tool, `openclaw.json` never contains it and the model never emits `tool=...dummy`. Also assert `openclaw mcp doctor --probe` output lists only approved servers |
| T7 | Allow-list | Approve a record whose URL is an `https://example.invalid/mcp` host: `[registry] skipped ...: host not allowed`, no `mcp.servers` entry, no outbound request to that host (VPC flow logs / no `bearer` in the container log for it) |
| T8 | Fail open | Point `AGENTCORE_REGISTRY_MCP_URL` at an unroutable host for one deploy: boot unchanged, `[registry] catalog unavailable`, static Gateway tools still 4/4 |
| T9 | Mid-session churn | Approve a record while 5 sessions are active: count `Tool ... not found` fallbacks over the next 20 prompts. Report the rate; if it exceeds 1 in 20, defer the refresh to the next cold start instead of hot-applying |
| T10 | Flag off | `enable_registry: false`: eight existing templates and `OpenClawGateway` byte-identical to `main`; `openclaw.json` unchanged (synth test + `bridge/registry-catalog.test.js`) |

Unit tests (no AWS): `bridge/registry-catalog.test.js` (record -> server entry; allow-list rejects
foreign and look-alike hosts; `toolFilter` derived; malformed record skipped; `rewriteBearer`
loops over N servers) and `tests/test_registry_stack_synth.py` (flag off = unchanged; flag on =
registry + record + curator role + no `agent-registry:` grant on the runtime, Gateway or Lambda
roles; cdk-nag clean).

## 7. Success criteria, cost, rollback, teardown

**Success** (all must hold): T1 through T10 pass; a tool added by approving a record reached
OpenClaw with zero image builds; the curator and deploy principals are different IAM roles and
CloudTrail shows `UpdateRegistryRecordStatus` from the curator role only; the T9 churn rate is
acceptable or the refresh has been moved to cold start.

**Cost.** $0 for the pilot and for any plausible size of this project: tens of records against a
5,000 free tier, one list + one batch-get per cold start against 2,000,000 free list/get calls per
month, and no searches ([pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)). Estimate:
even 1,000 cold starts a day is about 60,000 calls a month, 3 % of the free tier. The EventBridge
rule and SNS topic are within their free tiers at this volume. No new compute.

**Rollback during the pilot** (runtime side first, then the stack, the same order as the Gateway):

1. `cdk.json`: `enable_registry: false`; `./scripts/deploy.sh --runtime-only` redeploys the
   runtime without `AGENTCORE_REGISTRY_MCP_URL`; the bridge writes the static entry only. This
   alone restores today's behaviour for every new cold start.
2. `cdk destroy OpenClawRegistry` removes the registry, its records, the curator role and the
   notification rule. It holds no user data: records are metadata, and files and schedules
   never touched the Registry.
3. If the preview namespace was ever used (it should not be; the stack targets `agent-registry`),
   note that it disappears on 2026-10-30 regardless
   ([registry-faq](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html)).

**Teardown of a single bad tool** without touching infrastructure: curator rejects the record;
every cold start after that omits it (T4). No deploy needed.

## 8. Open questions

Carried over from the research; two are now answered from public documentation and are marked so.

1. ~~CloudFormation support for the `agent-registry` namespace.~~ **Answered:**
   `AWS::AgentRegistry::Registry` and `AWS::AgentRegistry::RegistryRecord` exist (section 2).
   Still open: whether the pinned `aws-cdk-lib` exposes them as L1 constructs, or `CfnResource` is
   needed.
2. ~~Does a Node SDK exist?~~ **Partly answered:** `@aws-sdk/client-agent-registry` is published
   on npm (latest 3.1140.0 on 2026-09-25,
   [npm](https://www.npmjs.com/package/@aws-sdk/client-agent-registry)). It signs with SigV4,
   which the per-user JWT model in section 4 does not want, so the plan calls the registry MCP
   endpoint over plain HTTPS with the bearer and does not add the SDK to the image.
3. **Does the Registry's JWT authorizer accept a Cognito access token via `allowed_clients`** the
   way the Gateway does (the Gateway needed the access token, not the ID token)? T2 settles it.
4. **Measured lookup latency from a microVM in the VPC.** T1 settles it; 0.2 to 0.6 s is an
   estimate until then.
5. **Can record synchronization target a `CUSTOM_JWT` Gateway** with an OAuth credential
   provider? Not needed for the pilot (tool list copied from `tool-schemas.json`); decide before
   adoption.
6. **Cross-deployment sharing mechanics.** RAM share with the Publisher permission is documented;
   whether a second deployment's stack can create a record in the hub registry through
   CloudFormation cross-account (`RegistryId` accepts an ARN per the template reference) is
   untested until there is a second deployment.
7. **Mid-session tool-set churn.** How often does a hot-applied `mcp.servers` change cost a model
   turn? T9 measures it and decides between hot-apply and next-cold-start.
8. **awslabs samples still use `bedrock-agentcore:*` IAM actions** (old namespace) in
   `discovery-and-invocation-at-runtime`; do not copy their IAM statements without migrating them
   ([registry-faq](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html)).

Skill persistence and any Registry role for skills are explicitly *not* on this list: they belong
to the separate skills work and are out of scope here (section 1).

## Sources

AWS documentation (fetched 2026-09-25):
[registry](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html),
[registry-key-capabilities](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-key-capabilities.html),
[registry-concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-concepts.html),
[registry-supported-record-types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-supported-record-types.html),
[registry-record-lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-record-lifecycle.html),
[registry-mcp-endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-mcp-endpoint.html),
[registry-supported-auth-types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-supported-auth-types.html),
[registry-cross-account-sharing](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-cross-account-sharing.html),
[registry-iam-permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-iam-permissions.html),
[registry-faq](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html),
[agentcore-regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html),
[AWS::AgentRegistry::Registry](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-agentregistry-registry.html),
[AWS::AgentRegistry::RegistryRecord](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-agentregistry-registryrecord.html),
[AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/).
Samples: [awslabs/amazon-bedrock-agentcore-samples 03-registry](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-features/07-centralize-and-govern-your-ai-infrastructure/03-registry).
Repo (`main` `c042866`): `docs/gateway-mcp-tools.md`, `stacks/gateway_stack.py`,
`bridge/gateway-mcp.js`, `bridge/agentcore-contract.js`, `scripts/deploy.sh`, `app.py`, `cdk.json`.
