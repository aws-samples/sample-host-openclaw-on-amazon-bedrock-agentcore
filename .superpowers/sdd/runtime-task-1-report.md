# Runtime Hardening Task 1 implementation report

## RED evidence

Recorded before any Task 1 production code was added.

### Node focused contract

```text
PATH="/opt/homebrew/opt/node@24/bin:$PATH" node --test --test-concurrency=1 \
  runtime-policy.test.js plugins/personal-operator/index.test.js \
  lightweight-agent.test.js

tests 35
pass 5
fail 30
```

The failures were the expected missing-boundary failures: the runtime-policy
module and native plugin did not exist; the inherited lightweight runtime
still exposed 17 tools including schedule, marketplace-skill, API-key and
secret capabilities; and it still executed skill scripts through child
processes. The plugin tests also proved the ESM package/manifest and bounded
S3 implementation were absent.

### Static product contract

```text
PATH="/opt/homebrew/opt/node@24/bin:$PATH" .venv/bin/python -m pytest \
  tests/test_product_configuration.py -q

5 failed, 4 passed
```

The expected failures showed that Docker did not copy the reviewed plugin,
the inherited executable skill tree still existed, Telegram delivery and old
capabilities remained in the contract, and the README still described the
unsafe imported boundary.

## GREEN evidence

### Focused Node contract

```text
PATH="/opt/homebrew/opt/node@24/bin:$PATH" node --test \
  --test-concurrency=1 runtime-policy.test.js \
  plugins/personal-operator/index.test.js lightweight-agent.test.js

tests 36
pass 36
fail 0
```

This proves the exact effective tool surface, one-plugin configuration,
strict child environment, local gateway-token generation, shell-free
lightweight runtime, and the bounded server-prefixed S3 implementation.

### Complete bridge suite

```text
PATH="/opt/homebrew/opt/node@24/bin:$PATH" AWS_REGION=eu-west-1 npm test

tests 206
pass 206
fail 0
```

### Product/static contract

```text
PATH="/opt/homebrew/opt/node@24/bin:$PATH" \
  .venv/bin/python -m pytest tests/test_product_configuration.py -q

9 passed in 0.06s
```

Node syntax checks passed for `runtime-policy.js`,
`agentcore-contract.js`, `lightweight-agent.js`, and the plugin ESM entry.
`bash -n bridge/entrypoint.sh`, Python byte-compilation, the forbidden-source
searches, `npm ci --omit=dev --dry-run`, and `git diff --check` also passed.

## Pinned OpenClaw loader evidence

The exact pinned source checkout was verified at commit
`4bfaccafd62ac2ff2e70ca1decc40fb1297ab438`. Its frozen pnpm install,
supply-chain lock check, full package/unified build, runtime post-build checks,
plugin-SDK export check, and Control UI build completed with exit code 0.

The repository plugin directory was copied, unchanged, into that checkout and
loaded with the pinned CLI using dummy non-secret runtime configuration:

```text
openclaw plugins inspect personal-operator --runtime --json

status: loaded
origin: config
compatibility: []
diagnostics: []
toolNames:
  po_file_list
  po_file_read
  po_file_write
  po_file_delete
hooks: 0
services: 0
http routes: 0
MCP servers: 0
```

The pinned CLI exposed two important configuration semantics and both are now
encoded in production tests:

- `minimal` contributes only `session_status`; this OpenClaw version rejects
  `allow` and `alsoAllow` in the same scope. The valid exact effective set is
  therefore `minimal` plus the six reviewed `alsoAllow` entries.
- the default memory slot loads bundled `memory-core` even when omitted from
  `plugins.allow`. Setting `plugins.slots.memory` to `none` disables it.

A fresh `openclaw plugins list --enabled --json` then returned exactly one
enabled plugin: `personal-operator`.

## Concerns and follow-up

- This task freezes the executable surface; it does not claim the inherited
  AWS session policy is least privilege. Exact S3-only session authority and
  immutable user binding remain Runtime Hardening Task 2.
- Workspace restore, mounts, and lossless synchronization remain Runtime
  Hardening Task 3.
- Verification used no real credentials, data, bucket access, deployment, or
  outbound message delivery. The plugin loader instantiated its client but did
  not issue an S3 request.
- The full pinned build emitted non-fatal warnings for absent optional
  `node-llama-cpp` binaries for other platforms; OpenClaw's own build guard
  accepted those optional imports and the build exited 0.
- No container image was built or deployed in this task. The pinned source
  build and actual loader proof validate the package shape; staging remains a
  later explicit gate.
