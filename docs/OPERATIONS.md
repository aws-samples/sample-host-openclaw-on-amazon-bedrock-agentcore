# Personal Operator v1 Staging Operations

Personal Operator is pre-production. These procedures are a release and
incident runbook, not authorization to deploy, connect real accounts, or send
messages. The local gates use synthetic data and require no cloud credentials.

## Operational invariants

- Region is exactly `eu-west-1`.
- Registration remains invite-only and the browser capability remains off.
- The OpenClaw process receives no Telegram, Google, OpenAI, database, approval,
  or cross-user authority.
- Runtime sessions receive temporary S3 access for exactly one server-derived
  user prefix. The runtime has no STS authority and cannot supply a policy to
  the trusted credential broker. The broker strongly validates its signed
  capability against the exact live user, session, runtime ARN, and immutable
  release qualifier before deriving that policy. Credentials last no more than
  15 minutes, refresh single-flight every 10 minutes, and any broker or refresh
  failure quarantines the runtime.
- Telegram work is grouped by internal user ID in one FIFO queue. A platform
  update has one canonical trace/deduplication ID.
- A provider write requires a persisted exact action, a matching unexpired
  founder approval, and a unique dispatch fence.
- An ambiguous provider result is `UNCERTAIN`. Operators reconcile provider
  evidence; they never replay a write merely because a request timed out.
- Gmail and Telegram strongly recheck the account-deletion fence at the last
  application-controlled point before provider dispatch. If the fence is
  present or unavailable, there is no provider call. An HTTPS request that
  already crossed that point cannot be recalled, and the second purge does not
  reverse an external provider effect.
- Account deletion first persists a strong authority fence, then revokes web
  sessions and locally stored provider authority before runtime or storage
  removal. If runtime purge is unproven, deletion remains pending and fails
  closed.
- Identity deletion strongly establishes a permanent SHA-256 user marker before
  its first identity scan. Registration and cross-channel binding condition
  their atomic transactions on that marker and each hashed channel marker being
  absent. Deleting an owned channel atomically writes its marker and removes
  its forward mapping plus exact invitation; a remap or unproven result leaves
  the old account pending without deleting the other user's mapping.
- Each workspace purge strictly walks the exact user prefix, aborts every
  incomplete multipart upload before deleting object versions and delete
  markers, and accepts only exact per-object S3 deletion evidence. Malformed,
  repeated, cross-namespace, or ambiguous listing/deletion responses keep the
  account in pending reconciliation.
- Browser export is synchronous in v0. The final ZIP must be at most 4 MiB so
  its base64 API response remains below Lambda's 6 MiB response ceiling;
  larger valid datasets fail explicitly and require a future authenticated
  asynchronous delivery path.
- Active dashboards and alarms consume aggregate AWS service metrics only.
  They do not enable model invocation text or image payload logging, and the
  archived legacy token-monitoring stack is not active.
- For the exact configured founder, deletion schedules the dedicated send
  OAuth secret for deletion with a 7-day Secrets Manager recovery window
  before deleting local records. An ambiguous secret result is reconciled by
  exact secret identity and otherwise keeps deletion pending. This disables
  application access immediately but is not immediate byte erasure.
- Deletion completes only after a second full purge following a minimum
  30-minute credential-and-invocation drain period. The hourly maintenance
  Lambda performs that reconciliation with reserved concurrency one.
- Deletion removes active application records. It cannot selectively erase a
  Telegram update already held by SQS (4 days) or its DLQ (14 days), nor
  retained logs/backups. Personal Operator deletes its local Google credential;
  the user must revoke the provider grant separately in Google Account settings.

## Credential-free local gate

Use Node 24.15 or newer. On the development host the tested path is:

```bash
export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
```

Run the release-specific security and integration tests:

```bash
PYTHONPATH=lambda ./.venv/bin/python -m pytest tests/security tests/integration -q
```

Generate the deterministic dependency evidence into a disposable directory:

```bash
RELEASE_INVENTORY_DIR="$(mktemp -d)"
./.venv/bin/python scripts/generate-release-inventory.py \
  --output-dir "$RELEASE_INVENTORY_DIR"
```

Then run the repository aggregate gate:

```bash
./scripts/test-local.sh
git diff --check
```

The aggregate command installs both Node dependency trees from their lockfiles
with `npm ci --ignore-scripts`, then covers Python tests, serialized bridge
tests, web UI tests and production build, syntax/compilation, offline CDK
synthesis, and a zero-finding cdk-nag check. A local pass proves only
deterministic code and template behavior; it does not prove AWS, AgentCore,
OAuth, Telegram, Gmail, or OpenAI behavior.

## Docker-backed release-asset gate

Run this only from the clean commit proposed for release. First review and pin
the exact digest of the official AWS Lambda Python builder image; mutable tags
are rejected.

```bash
export PERSONAL_OPERATOR_RELEASE_ACCOUNT=123456789012
export PERSONAL_OPERATOR_RELEASE_COMMIT="$(git rev-parse HEAD)"
export TRUSTED_LAMBDA_BUILD_IMAGE='public.ecr.aws/lambda/python@sha256:<reviewed-digest>'
export PERSONAL_OPERATOR_RUNTIME_CONTEXT_FILE="$PWD/build/runtime-context.json"
export PERSONAL_OPERATOR_RUNTIME_IMAGE_URI='123456789012.dkr.ecr.eu-west-1.amazonaws.com/personal-operator/bridge@sha256:<reviewed-digest>'
./scripts/test-release-assets.sh
```

The gate runs the credential-free suite, builds the shared Lambda Python
3.13/ARM64 asset from the full transitive SHA-256 lock, verifies its file,
source, dependency, platform, handler-import, and builder inventories with
network disabled, and synthesizes a non-synthetic-account-shaped template
bound to the exact runtime-context v3 identity. It rejects every placeholder
runtime binding. It unsets AWS credentials and contains no deployment command.
If Docker, either reviewed digest, or the exact runtime context is unavailable,
this gate remains unclosed; source-only synth does not substitute for it.

## Staging preflight: prepare, do not deploy

The **staging deployment path implemented and locally verified; not deployed**
boundary is exact. The repository now contains the strict contracts, durable
journal, deterministic Lambda asset format, retained immutable ECR/signing
resources, direct AgentCore CloudFormation L1 resources, injected evidence
adapters, and phase CLI. No phase has been run against AWS.

`scripts/deploy.sh` is only a compatibility shim for
`scripts/staging-release.py`. Validation and state transitions live in the
Python package. The CLI surface is:

- `--preflight`: validate one clean commit/tree/account/region and create the
  canonical journal without discovering credentials;
- `--phase <foundation|image|runtime|endpoint|context|consumer-changesets|consumers|verify>`:
  run only the legal next phase after an exact mutation confirmation;
- `--resume <journal>`: resume only a stable journal;
- `--resume <journal> --reconcile --driver <reviewed-operation>`:
  run the exact operation's phase-specific read-only live observation for an
  `UNCERTAIN` journal. The observer, never the operator, proves `PERSISTED` or
  `ABSENT`;
- `--status <journal>`: print the canonical journal without credentials;
- `--rollback <verified-transaction-id>`: write rollback intent before dispatch
  and accept only the journal's exact rollback reference. It never retargets a
  retained release endpoint.

Credential-free preflight example:

```bash
export PERSONAL_OPERATOR_RELEASE_ACCOUNT=123456789012
export PERSONAL_OPERATOR_RELEASE_COMMIT="$(git rev-parse HEAD)"
JOURNAL="$PWD/build/releases/release_${PERSONAL_OPERATOR_RELEASE_COMMIT}.json"
./scripts/deploy.sh \
  --preflight \
  --journal "$JOURNAL" \
  --account "$PERSONAL_OPERATOR_RELEASE_ACCOUNT" \
  --commit "$PERSONAL_OPERATOR_RELEASE_COMMIT"
./scripts/deploy.sh --status "$JOURNAL"
```

Do not supply an operation or mutation confirmation during preflight. A real
phase requires one self-contained reviewed executable. The CLI hashes and
copies its exact bytes into a private file retained for that invocation, then
requires the exact
confirmation
`mutate:release_<40-sha>:<phase>:sha256:<operation-hex>`. The journal records
that operation digest while `UNCERTAIN`. For the first cloud phase it also
requires the exact commit/account/region/digest-bound rollback reference.

Immediately before each dispatch or observation, the CLI rejects conflicting
`CDK_DEFAULT_REGION`, `AWS_REGION`, or `AWS_DEFAULT_REGION`, authenticates the
exact account, and pins all three child variables to `eu-west-1`. Mutation must
return only `{"dispatched":true}`. It is never treated as persistence proof:
the same retained executable is called in read-only observation mode and must
return one canonical, identity-bound `personal-operator.phase-observation.v1`
record. A `PERSISTED` record contains only the evidence owned by that phase; an
`ABSENT` record contains none. The CLI then performs the matching journal
transition.

After a crash or ambiguous result, use the same exact operation bytes and the
confirmation
`reconcile:release_<40-sha>:<phase>:sha256:<operation-hex>`. There is no
operator `persisted|absent` switch and no local evidence-file override. A
changed operation, timeout, noncanonical observation, account/region mismatch,
unknown outcome, or wrong subject leaves the journal `UNCERTAIN` and blocks
every later phase. Rollback follows the same write-ahead mutation plus
authoritative observation rule, with
`rollback:release_<40-sha>:sha256:<operation-hex>`.

The CDK foundation owns exactly one private retained
`personal-operator/bridge` repository with immutable tags, scan-on-push, KMS
encryption/rotation, a frozen untagged lifecycle, one Notation OCI signing
profile, and one exact repository signing filter. Only
`ecr:GetAuthorizationToken` retains an unavoidable wildcard; runtime layer
pulls use the exact repository ARN.

An empty runtime context synthesizes no Runtime or RuntimeEndpoint. An exact
lowercase 40-character commit plus the canonical private-ECR `@sha256:` URI
synthesizes one stable `AWS::BedrockAgentCore::Runtime` and one retained
`AWS::BedrockAgentCore::RuntimeEndpoint` named `release_<40-sha>`. The direct L1
resource freezes the VPC, execution role, environment, `/mnt/workspace`, HTTP
protocol, and lifecycle values. The removed mutable AgentCore toolkit path must
not be restored.

The canonical `RuntimeContextV3` records the service runtime ID, endpoint ID,
endpoint name, exact versioned runtime ARN, and image digest. Consumer changes
accept it only when injected live evidence proves READY runtime/endpoint state,
the reviewed role and image, and equal endpoint live/target versions. A later
runtime version cannot silently move an existing release endpoint.

The shared trusted ZIP is consumed by five unique handler modules across six Lambda functions;
`web.index.lambda_handler` is intentionally used by both the
web and maintenance functions. The Docker-backed gate must import all five
handlers from the exact Python 3.13/ARM64 ZIP with networking disabled.

### External staging gates — all open

- OPEN — runtime image push
- OPEN — managed signing
- OPEN — authoritative image scan
- OPEN — CloudFormation change-set execution
- OPEN — AgentCore runtime readiness
- OPEN — consumer application
- OPEN — moderated pilot

Also open: the Docker-backed Lambda import proof, real account/region and IAM
inspection, storage/recovery evidence, provider credentials/callbacks, and any
real founder effect. Stop at the first mismatch. Never use a real message or
email send to discover whether infrastructure is wired correctly.

Rotating the CloudFront origin-verification secret also requires a Web stack
update: CloudFront resolves the value during deployment while the web Lambda
loads it from Secrets Manager. Treat the pair as one coordinated rotation.

## Incident procedures

### Gmail action is `UNCERTAIN`

1. Freeze that action; do not create a replacement or retry it.
2. Use the deterministic RFC 822 Message-ID already persisted on the action.
3. Query the exact approved Gmail account for that ID.
4. Confirm only if there is exactly one `SENT` message whose account, sender,
   recipient, plain-text payload hash, thread ID, and provider time all match.
5. If evidence is absent, leave the action `UNCERTAIN` until a documented human
   decision. Absence is not proof that Gmail did not accept the send.

### Telegram delivery is uncertain

Do not resend automatically. The durable outbox records an uncertain terminal
state because Telegram has no application idempotency key. Inspect provider
history and decide manually whether to send a visibly new follow-up.

### Runtime is quarantined or storage commit is unproven

Stop new turns for that user. Preserve the current and parent S3 generations,
the pointer, and logs. Reconcile the compare-and-swap pointer before restart.
Never repair a pointer by selecting the newest object timestamp.

### Deletion remains pending

Keep the hashed deletion intent and local authority revocation in place. If the
intent is `PENDING`, reconcile the runtime stop/tombstone and first purge. If it
is `FINALIZING`, wait for the 30-minute drain boundary and let the singleton
hourly maintenance path repeat the entire purge before completing the minimal
tombstone. Never remove S3 while a runtime may still repopulate the namespace,
and never manually skip the second purge.

If the affected user is the configured founder, also inspect only the metadata
for the exact founder-send secret. It must be scheduled for deletion or absent;
never force-delete it merely to clear the incident. The 7-day recovery window
is a deliberate operational boundary, and the remote Google grant still needs
separate revocation in Google Account settings.

### Suspected credential exposure

Disable the affected provider connection and founder effect path, revoke web
sessions, rotate only the exposed domain-specific secret, and inspect access
logs. Rotating one secret must not silently substitute it for another purpose.
Do not copy secret values into tickets, incident chat, or release evidence.

## Recovery evidence

For every incident or release, retain only non-secret evidence: candidate
commit, image digest, stack/template hashes, command and test results, action or
trace IDs, state transitions, provider receipt IDs, timestamps, and the name of
the human approver. Never retain raw Gmail bodies, OAuth codes/tokens, cookies,
approval bearer tokens, Telegram bot tokens, or decrypted workspace content in
the operations ledger.
