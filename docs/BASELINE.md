# Reproducible Local Baseline

This ledger records the imported sample state and Task 1 development evidence.
It is not deployment or production evidence. No AWS credentials, cloud
deployment, image push, or real message is used by these commands.

## Source point

- AWS sample commit: `e13e385ec44a3776e571ec48001904e9394cc20e`
- Import date: 2026-07-17
- Product branch: `codex/personal-operator-v0`
- License: MIT No Attribution (MIT-0), retained in `LICENSE`

See `docs/UPSTREAM.md` for the complete source ledger.

## Local toolchain

- Host: macOS on Apple Silicon
- Python: 3.12.0 in `.venv`
- pytest: 9.1.1
- Imported default shell toolchain: Node.js 22.15.0, npm 10.9.2
- Task 1 verification toolchain: Node.js 24.18.0, npm 11.16.0 from
  `/opt/homebrew/opt/node@24/bin`
- Required image floor: Node.js 24.15.0
- Docker, AWS CLI, and a global CDK CLI were not available locally

Use the Node 24 toolchain for local verification on this host:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" ./scripts/test-local.sh
```

## Imported evidence before product changes

- Router unit tests: 151 passed, 1 failed. The failing test was
  `TestFullPipeline.test_regex_fallback_for_malformed_json`; the imported
  fallback returned an empty string for a malformed trailing-comma content
  block.
- Bridge suite under normal parallel discovery with `AWS_REGION=eu-west-1`:
  335 passed, 3 failed. One failure involved the shared browser-session file.
- Bridge suite serialized with `--test-concurrency=1`: 336 passed, 2 failed.
  Both failures were in the API-key subsystem that Task 2 removes from the
  runtime.
- CDK CLI synthesis had no configured AWS credentials and also surfaced the
  imported `AwsSolutions-COG8` Cognito feature-plan finding.

These are inherited defects or environment constraints, not evidence for
Personal Operator behavior.

## Task 1 TDD evidence

RED command:

```bash
./.venv/bin/python -m pytest tests/test_product_configuration.py -v
```

Initial result: 3 failed. The failures proved that the imported sample still
had an empty region, no bridge Node engine contract, and marketplace
installation in the image. The test also covered the model, session defaults,
30-day retention, invite-only registration, disabled browser, empty runtime
IDs, image version, Node base image, and the immutable OpenClaw source pin.

GREEN command:

```bash
./.venv/bin/python -m pytest tests/test_product_configuration.py -v
```

Result after the minimal configuration changes: 3 passed in 0.01 seconds.

## Aggregate Task 1 evidence

Command:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" ./scripts/test-local.sh
```

The aggregate command intentionally exits non-zero while inherited failures
remain. Its Task 1 run produced:

- Node.js version contract: passed with Node.js 24.18.0.
- Python unit tests: 154 passed, 1 failed. The 154 passes include the three new
  product-configuration tests; the one failure is the inherited malformed-JSON
  router case described above.
- Bridge Node tests, serialized and fixed to `AWS_REGION=eu-west-1`: 336 passed,
  2 failed. The exact failures are
  `falls back to native file when SM unavailable` and
  `returns error when key not found anywhere` in `retrieve_api_key`.
- JavaScript syntax checks: passed.
- Python compilation checks: passed.
- Credential-free CDK assembly synthesis with synthetic account
  `000000000000`: passed.
- CDK cdk-nag contract: failed only on the inherited
  `AwsSolutions-COG8` finding for `OpenClawSecurity/IdentityPool/Resource`.

Task 1 does not modify the API-key subsystem to force the two bridge tests
green; Task 2 removes that subsystem under its own tests. The router fallback
and Cognito finding remain explicit imported failures. A container build was
not run because Docker is unavailable on this host; the Docker source/version
contract is covered statically, and a real image build remains required before
release evidence can be claimed.

## Commands

Focused product contract:

```bash
./.venv/bin/python -m pytest tests/test_product_configuration.py -v
```

Complete credential-free local baseline:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" ./scripts/test-local.sh
```

Individual bridge suite:

```bash
cd bridge
AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1 \
  PATH="/opt/homebrew/opt/node@24/bin:$PATH" npm test
```

The local script is the canonical aggregate command. It runs all steps even
when one fails and exits non-zero if any contract is not clean.
