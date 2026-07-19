# Personal Operator v1 Implementation Evidence

## Verdict

Personal Operator v1 is implemented and locally verified pre-production
source. This ledger is **local and synthetic evidence only**. It is not
authorization to deploy, enable provider effects, connect a real account, or
run a pilot.

No image, stack, runtime, connector, browser, compute job, provider request, or
real message is claimed by this document. The exact commit and tree are
recorded after the terminal commit because a Git commit cannot contain its own
identity. Region scope is exactly `eu-west-1`; evidence date is 2026-07-19.

## Evidence subject

- Clean Task 12 starting commit:
  `f0b5225c64ecfc2c9e57817f56d5abd231a587bc`
- Starting tree: `9cc9747630073c8b93482325d329f52511f30953`
- Phase 4 integrated audit commit:
  `5d5020f2cf6d9ca7c074209cd359dec073ceccf8`
- Task-3 lifecycle hardening commit:
  `68bc13524bd5218c5d920475b82f20e9e0bcc29f`
- Task 11 operational-evidence commit:
  `f0b5225c64ecfc2c9e57817f56d5abd231a587bc`
- Local Python: 3.12.0; pytest: 9.1.1
- Local Node: 24.x from `/opt/homebrew/opt/node@24`

The authoritative final commit/tree and the terminal log digest are reported
with the handoff after the commit is created. Earlier dirty-tree results remain
development evidence only.

## Implemented consumer surface

The synthetic, credential-free application journey covers invite, browser
connect, read-only Gmail OAuth, scan, opportunity card, feedback, local draft
and workspace editing, proposal-only scheduling, contained compute, portable
export/import, replay denial, and two-pass deletion. The web surface also
implements sessions, CSRF protection, approval preview/control, deterministic
export, retention maintenance, and deletion reconciliation.

The model-visible catalog is exactly:

1. `po_file_list`
2. `po_file_read`
3. `po_file_write`
4. `po_file_delete`
5. `po_web_read`
6. `po_schedule_list`
7. `po_schedule_propose`
8. `po_schedule_cancel_propose`
9. `po_compute_run`
10. `po_compute_status`

The runtime holds no provider, messaging, connector, approval, or browser
credential. Workspace AWS credentials are short-lived, namespace-scoped, and
issued by the trusted broker only after exact session admission. Public web
reads require a current authenticated request's expiring target grant.
Schedule authority stays in the trusted control plane. Both compute operations
return `ADAPTER_DISABLED` in active composition.

## Integrated invariant audit

| Invariant | Local evidence |
|---|---|
| Catalog/package/runtime parity | Source catalog, compiled catalog, schemas, plugin manifest, OpenClaw policy, gateway, and packaging inventory bind the same ten operations and digests. |
| Minimal runtime IAM | AgentCore role contains no workspace S3, STS, DynamoDB, Scheduler, EventBridge, connector, browser, or compute authority. Its trust is restricted to AgentCore with account/ARN confused-deputy conditions. |
| No browser or dynamic MCP exposure | Browser/MCP tools are absent from the model-visible catalog and registration policy; repository-owned bridge dependencies contain no browser package, and connector/Browser Gateway composition is disabled. The full pinned upstream image dependency/SBOM inspection remains an external gate. |
| Disabled effect planes | Active connector and browser composition is disabled. The Gmail executor crosses the complete Task-3 admission, idempotency, deletion, approval, and uncertainty kernel. The latent synthetic MCP/browser adapter remains a before-enabling gate because equivalent approval, one-time-use, and expiry enforcement is incomplete. |
| Scheduled turns | Trusted envelopes require `externalEffects=false`; only catalog-derived read and proposal operations are admitted. |
| Networkless compute | Production composition creates no compute stack, credentials, launcher, or transport and returns `ADAPTER_DISABLED`; this is containment, not isolation proof. |
| Portable import | Imported schedules land inert, past effects are non-replayable, identical activation is denied, and activation remains tenant-bound. |
| Tenant and target grants | Durable state, workspaces, exports, imports, schedules, and one-use web targets are tenant/request bound and strongly reread. |
| Uncertain effects and deletion | Ambiguous writes become no-replay `UNCERTAIN`; deletion fences authority first and completes only after the drain and second purge. |

The detailed Phase 4 finding/fix ledger is
[v1-phase-4-integrated-audit.md](../.superpowers/sdd/v1-phase-4-integrated-audit.md).

## Task 11 operational evidence

The standalone harness ran three isolated synthetic participants. It produced
17 aggregate rows representing 51 successful or deliberately contained events
and no external call. The canonical report was 2,706 bytes with SHA-256
`634ef180c3eeadf5cff07fc8d16a78c532271aedf2a68ac1c4c51fef44fd7e37`.
The same journey ran twice with byte-identical stdout and empty stderr.

The report contains only participant count plus bounded component, operation,
outcome, and count dimensions. Tests reject participant identifiers, invite
bearers, addresses, subjects, excerpts, source/thread/message identifiers,
workspace paths/content, provider values, and credential canaries. Raw socket
connect/send operations and all injected provider ledgers are denied or empty.

Compute contributes an `ADAPTER_DISABLED` containment event; it is not live
compute-isolation evidence. Only the existing DLQ and native maintenance
heartbeat have truthful live AWS metric producers. The other alarm contracts
remain synthetic until a deployed metric transition is observed.

## Task 12 hardening findings resolved locally

- AgentCore execution-role trust now names only
  `bedrock-agentcore.amazonaws.com` and binds exact source account and regional
  AgentCore ARN.
- Runtime networking has HTTPS egress only to the trusted interface-endpoint
  group and the exact `eu-west-1` S3 managed prefix list, with zero ingress.
  All S3 names use the gateway route; its exact policy admits regional ECR
  layer reads, the service-principal-conditioned AgentCore managed-session
  bucket operations, and deterministic workspace-bucket operations that IAM
  and the broker's STS session policy further narrow per user.
- Live AgentCore evidence accepts and canonicalizes away only literal
  `requireServiceS3Endpoint=false`; unsafe values fail, while missing live
  disposition is ambiguous pending authoritative creation evidence.
- Router and web API access logs retain only method, fixed route, status,
  response length, and latency. Web/Gmail warnings use fixed canonical JSON and
  contain no dynamic exception name, message, request identifier, address,
  path, source, or content.
- Runtime synthesis sets `DISABLE_ADOT_OBSERVABILITY=true`, disabling
  payload-rich AgentCore application observability while preserving ordinary
  platform operational logs. Exact live retained-field inspection remains an
  OPEN deployment gate.
- The runtime-image recipe declares numeric UID/GID `1000:1000`, recursively
  removes write bits from `/app`, `/opt/openclaw`, the immutable seed, and the
  unused base-image home, and removes the base image's `/var/tmp` write bit.
  Entrypoint source rejects a changed identity or a writable checked top-level
  image path before loading trusted code. `/run`, `/tmp`, platform scratch
  mounts, and the invocation-time workspace are ephemeral writable areas. An
  exact built-image filesystem probe remains required before publication.
- Retained AgentCore resource policies on both the runtime and immutable
  endpoint explicitly deny `InvokeAgentRuntimeCommand` and
  `InvokeAgentRuntimeCommandShell`. The live AgentCore evidence adapter accepts
  only those exact deny documents, so the platform command surface cannot
  silently bypass the ten-tool catalog.
- The old architecture files are explicitly archived; README and privacy/
  capability boundaries now describe the exact v1 surface.

## Focused and aggregate verification

The final candidate is verified with:

```bash
/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python \
  -m pytest tests/security tests/integration \
  lambda/capabilities lambda/scheduler lambda/portable lambda/connectors \
  lambda/browser lambda/compute -q
```

The complete aggregate is run with:

```bash
PYTHON=/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python \
PATH="/opt/homebrew/opt/node@24/bin:$PATH" \
./scripts/test-local.sh 2>&1 | tee /tmp/personal-operator-v1-task12.log
grep -Fx 'All local checks passed.' /tmp/personal-operator-v1-task12.log
```

The acceptance sentinel is the literal final line `All local checks passed.`;
shell status alone is not accepted. Candidate counts, digests, and terminal
review verdicts are recorded only from the final reviewed subject.

| Verification record | Result |
|---|---|
| Focused security/integration | Focused security/integration: `661 passed` |
| Aggregate Python | Aggregate Python: `2135 passed, 10 subtests passed` |
| Aggregate log | Aggregate log SHA-256: `073325584f41ac3b1784a6bd766263900cee9da6c26378d33e617c3514980103` |
| Specification review | Independent specification review: `ACCEPT` — no unresolved Critical, High, Important, or Minor finding |
| Security review | Independent security review: `ACCEPT` — no unresolved Critical or Important finding |

The recorded aggregate log digest is replaced from the first complete
post-review candidate run, then a second clean aggregate run confirms the final
ledger-only update. The post-commit terminal log digest is necessarily reported
outside its own Git subject.

## Static release audit

Every category below is covered by a scoped source search plus executable
mutation or hostile-path tests. Expected references in tests, frozen contracts,
and fail-closed adapters are distinguished from unexpected release authority.

| Search category | Accepted result |
|---|---|
| credential | No high-confidence production literal; provider and signing fields cannot cross the runtime or queue boundary. |
| forbidden runtime capability | The effective ten-tool policy exposes no shell, browser, dynamic plugin/MCP, scheduler SDK, Secrets Manager SDK, or marketplace capability. Repository-owned bridge dependencies contain no active browser package; full upstream image dependency inspection remains OPEN. |
| dynamic MCP | Only locked manifest parsing and disabled synthetic adapter references; no model-selected server/tool registration. |
| browser IAM | Browser authority exists only in the separately disabled browser stack and is absent from active composition. |
| networkless compute | No active compute stack/resource/role/launcher identifier survives synthesis except the exactly validated isolation alarm. |
| catalog parity | Exactly ten tool names and exact schema digests across source, compiler, gateway, package, plugin, and runtime policy. |
| cross-tenant | Tenant/cartesian canaries fail before state, workspace, schedule, target, import/export, or adapter use. |
| target grant | Public reads require the exact current invocation, tenant binding, normalized URL hash, expiry, and one-time claim. |
| schedule effect | Scheduled envelopes require `externalEffects=false`; connector/browser/provider effects are rejected. |
| import replay | Imported effect history is inert and identical activation is rejected. |
| log content | API access fields and application-emitted message fields use closed metadata schemas; content, identity, paths, and dynamic exception data are absent. Payload-rich AgentCore application observability is disabled with `DISABLE_ADOT_OBSERVABILITY=true`. AWS platform envelopes and system records still add operational request/timing metadata, so live CloudWatch inspection remains OPEN. |

The exact candidate searches were:

```bash
rg -l --hidden -g '!node_modules/**' -g '!web/node_modules/**' \
  -g '!.git/**' -g '!cdk.out/**' -g '!build/**' -g '!redteam/**' \
  '(AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN ([A-Z ]+)?PRIVATE KEY-----|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|[0-9]{6,12}:[A-Za-z0-9_-]{30,}' \
  bridge lambda stacks release_tools scripts specs docs README.md tests | sort

rg -l -g '*.js' -g '*.json' -g 'Dockerfile*' \
  '(playwright|puppeteer|clawhub|mcpServers|browser\.|computer\.|execFile|execSync|child_process|fetch\(|https?://)' \
  bridge | sort

rg -n 'InvokeAgentRuntimeCommand(Shell)?' \
  app.py stacks release_tools lambda bridge tests docs README.md | sort

rg -l '(SyntheticMcpConnectorAdapter|mcpServers|dynamic MCP|dynamic_mcp|MCP_SERVER|clawhub)' \
  app.py bridge lambda stacks tests docs README.md | sort

rg -l '(BrowserStack|enable_browser|browser_actions|bedrock-agentcore.*browser|browser.*IAM|browser.*iam|Browser Gateway)' \
  app.py stacks tests docs README.md | sort

rg -l '(ADAPTER_DISABLED|networkless|network.?none|ComputeStack|compute_adapters|AWS::ECS|AWS::EC2::NetworkInterface)' \
  app.py lambda stacks tests docs README.md | sort

shasum -a 256 specs/capabilities/catalog-v1.json bridge/capabilities/catalog-v1.json
diff -rq specs/capabilities/schemas bridge/capabilities/schemas
find specs/capabilities/schemas -type f -name '*.json' | wc -l
find bridge/capabilities/schemas -type f -name '*.json' | wc -l

rg -l '(cross.?tenant|wrong.?tenant|tenant.?mismatch|user_beta|different tenant)' \
  lambda tests bridge | sort

rg -l '(target.?grant|targetGrant|current.?request|prior.?request|private.?DNS|redirect.?rebind)' \
  lambda tests bridge specs docs | sort

rg -l '(externalEffects|READ_ONLY_AGENT_TURN|scheduled.*effect|schedule.*read.?only|no.?effects)' \
  lambda tests bridge specs docs | sort

rg -l '(non.?replay|replayable|activatedBundleHashes|already activated|import.*replay|schedules.*DISABLED|connectors.*DISCONNECTED)' \
  lambda tests specs docs | sort

rg -l '(requestId|sourceIp|requestTime|integrationErrorMessage|LOG_EVENT_REJECTED|MetadataOnly|metadata.?only|child output|log.*content|content.*log)' \
  bridge lambda stacks tests docs README.md | sort
rg -n 'AccessLogFormat|access_log|accessLog|routeKey|responseLatency|responseLength' \
  stacks/router_stack.py stacks/web_stack.py
```

The credential search and the matching executable scanner returned only
synthetic test fixtures and no production-source literal. The
forbidden-runtime search returned 37 reviewed repository bridge paths: schemas
and tests plus fixed child-process/loopback implementations, package-lock URLs,
and the Docker build-time source URL. Within that repository-owned bridge
surface it found no active Playwright/Puppeteer import, ClawHub/dynamic-MCP
registration, browser/computer tool, generic `fetch`, or shell-exec capability.
This source search does not inspect the full pinned upstream checkout copied at
image build time; exact image SBOM/dependency inspection remains OPEN.
The AgentCore command-API search returned 11 matches, all in the two explicit
deny documents, exact live-policy parser, hostile tests, or this ledger; it
returned no identity-policy allow.
Dynamic-MCP and browser-IAM searches returned 23 and 15 paths respectively;
`app.py` was absent from both, and production matches were confined to disabled
standalone source. The compute search returned 29 paths; `app.py` was absent and
active-composition tests require `ADAPTER_DISABLED` with no compute resource or
launcher.

The catalog files both hashed to
`b4385b54dfa5aaa7ecf2e916111e44248b647b15208432bb9d31883c26e87a26`;
schema diff was empty and both trees contained 20 JSON schemas. Cross-tenant,
target-grant, schedule-effect, import-replay, and log-content searches returned
18, 21, 40, 19, and 18 reviewed paths. Their production matches were the
guarded implementations; associated hostile tests cover tenant Cartesian
denial, exact-current-request grants, `externalEffects=false`, inert one-time
import activation, and closed metadata logging. The API-log field search
returned only the two fixed access-log definitions and related assertions;
neither API access-log definition contains request ID, source IP, request time, raw path,
payload, or integration-error fields.

## Known-open implementation and external gates

The current release CLI still requires a separately reviewed deploy-time
rewrite so every mutation phase consumes the in-package live evidence composer
and reconciles uncertainty authoritatively. The connector adapter still needs
Gmail-executor-equivalent approval, one-time-use, and expiry enforcement before
the connector or Browser Gateway plane can be enabled.

| Gate | State and missing evidence |
|---|---|
| OPEN — runtime image push | No exact commit-tagged private-ECR digest has been published. |
| OPEN — managed signing | No completed managed Notation signature has been inspected. |
| OPEN — authoritative image scan | No live scan/SBOM/provenance referrer set has been inspected. |
| OPEN — CloudFormation change-set execution | No reviewed exact-account change set has been executed. |
| OPEN — AgentCore runtime readiness | No exact runtime/version/image/role/network/storage READY state has been observed. |
| OPEN — consumer application | No consumer stacks have been applied to an exact live runtime context. |
| OPEN — connector/provider effects | No real provider credential or provider effect is authorized. |
| OPEN — Browser Gateway | The plane is disabled and approval enforcement is incomplete. |
| OPEN — networkless compute | No production adapter, pinned workload image, launcher, or live isolation proof exists. |
| OPEN — moderated pilot | No real participant, cost/latency result, or pilot authorization exists. |

These gates must not be inferred from local code shape, synthesis, or synthetic
journeys.
