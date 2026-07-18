# Personal Operator v0 Operations

Personal Operator v0 is pre-production. These procedures are a release and
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

Perform this checklist against a dedicated non-production AWS account. Stop at
the first mismatch. The final deployment command is deliberately not part of
the preflight.

1. Freeze one clean candidate commit. Record `git rev-parse HEAD`, the Docker
   context digest, the OpenClaw source commit, Node base image, CDK context, and
   generated SBOM hashes. Do not release a dirty tree.
2. Confirm local tools: Python 3.12+, Node 24.15+, Docker with ARM64 build
   support, AWS CLI, and CDK CLI. The AgentCore toolkit is not an accepted
   runtime deployment boundary.
3. Resolve the AWS account through `aws sts get-caller-identity`. Require an
   explicit allowlisted staging account and `eu-west-1`; never infer a target
   from a developer default profile.
4. Run `scripts/test-release-assets.sh`. Prove that `boto3`, `cryptography`,
   `google-auth`, `google-api-python-client`, `openai`, and all four Lambda
   handlers import from the exact Amazon Linux/ARM64 asset. Record
   `MANIFEST.json` and `ASSET.sha256`; do not regenerate the lock casually.
5. Build the bridge image from the frozen Dockerfile. Scan it, reject critical
   or high unreviewed findings, push it only after explicit authorization, and
   record the immutable private-ECR `@sha256:` URI. Tag that exact digest
   `commit-<candidate-commit>`; the deployment preflight verifies both the
   digest and commit tag. A mutable tag or numeric `image_version` is not
   release identity.
6. Synthesize with the exact staging account and inspect every IAM statement,
   route, secret ARN, bucket, table, alarm, retention rule, and cdk-nag
   suppression. No wildcard may grant runtime/provider/action authority.
7. Populate separate Google read-only and founder-send OAuth secrets. Validate
   their JSON schemas without printing values. External pilot identities must
   never be included in the founder allowlist or receive send scope.
8. Register only the exact emitted HTTPS OAuth callback. Confirm the Telegram
   webhook secret, web origin, approval key, session key, and provider secrets
   are separate and KMS protected.
9. Require a CloudFront-scope WAF ARN, API throttling, retained access logs,
   alarms, S3 versioning, public-access blocks, and DynamoDB point-in-time
   recovery before accepting a pilot.
10. Run three synthetic users through connect, runtime replacement, export, and
    deletion. Run one synthetic provider timeout and reconcile it. Inspect logs
    for raw email, bearer approval links, cookies, OAuth codes, credentials,
    and cross-user identifiers.
11. A real founder Gmail send is a separate human gate. It requires the exact
    account, recipient, subject, body hash, approval expiry, candidate commit,
    image digest, and rollback owner. Never use a real send to discover whether
    the infrastructure is wired correctly.

`scripts/deploy.sh` can change cloud state and create paid resources in its CDK
modes. Do not
run it as a preflight or from automation without fresh explicit authorization.
It rejects unknown modes, a dirty tree, a mutable builder, an
inferred account, an account that differs from STS, and a missing global WAF.

Runtime deployment is intentionally **not implemented**. `--full` and
`--runtime-only` fail before account validation, credential discovery,
preflight, or any cloud call. The removed toolkit flow first deployed mutable
source and only then changed the runtime to the reviewed digest, leaving an
unreviewed intermediate version. Do not restore that flow. A future runtime
provisioner must create or update AgentCore directly from the reviewed private
ECR digest and prove the exact artifact before enabling either mode.

The following values remain the required release binding for the available CDK
modes and for a future direct immutable runtime provisioner:

```bash
export PERSONAL_OPERATOR_DEPLOY_ACCOUNT=123456789012
export PERSONAL_OPERATOR_DEPLOY_COMMIT="$(git rev-parse HEAD)"
export PERSONAL_OPERATOR_DEPLOY_CONFIRMATION='deploy:123456789012:eu-west-1'
export PERSONAL_OPERATOR_RUNTIME_IMAGE_URI='123456789012.dkr.ecr.eu-west-1.amazonaws.com/personal-operator/bridge@sha256:<reviewed-digest>'
export TRUSTED_LAMBDA_BUILD_IMAGE='public.ecr.aws/lambda/python@sha256:<reviewed-digest>'
# --full and --runtime-only deliberately stop here with no cloud changes.
./scripts/deploy.sh --full
```

`--phase1`, `--phase3`, and `--cdk-only` remain cloud-mutating CDK operations
and still pause for permission broadening. Phase 3 only accepts a separately
provisioned runtime whose candidate-bound context and authoritative metadata
match the exact reviewed digest and whose storage contract verifies. This repo
does not currently create that context or runtime, so the end-to-end product is
not deployable from this script.

The external provisioner must write `build/runtime-context.json` with schema
`personal-operator.runtime-context.v3`. Its endpoint name is exactly
`release_<40-character-lowercase-candidate-commit>`; `DEFAULT`, aliases, tags,
and user-selected names are rejected. The context separately records the
service endpoint ID and runtime version. Phase 3 accepts it only when the
version equals the reviewed runtime ARN suffix and the service reports that
the exact endpoint ID and name have both `liveVersion` and `targetVersion`
equal to that version. Consumers receive only this release endpoint, including
their IAM runtime-endpoint resource. A later runtime version therefore cannot
silently move an existing release.

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
