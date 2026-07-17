# Runtime Hardening Task 1 implementation report

## RH1 review-fix closure (2026-07-17)

The review-fix wave closes the effective-capability gaps left by the initial
Task 1 implementation:

- `agents.defaults.skills` and `skills.allowBundled` are both empty. The exact
  pinned CLI returned an empty eligible skill inventory, so no skill text is
  model-visible.
- text commands are disabled. The WebSocket bridge now uses pinned protocol 4,
  identifies as `gateway-client`, and requests only `operator.read` and
  `operator.write`. A real pinned gateway rejected `config.patch` with
  `missing scope: operator.admin`; the config SHA-256 remained
  `e7d4b56e58698acb329e419b1a5a9ba5c22b91b6d6323fd992e32a906b6abc23`
  before and after.
- file tools now address only `<workspacePrefix>/files/<relative path>` and
  reject traversal plus `.openclaw`, `_uploads`, `_internal`, and `internal`
  top-level paths.
- the proxy no longer seeds, reads, or promotes legacy workspace identity,
  persona, tools, or memory files into the system prompt. Obsolete skill
  diagnostics and the mirrored prompt tests were removed.
- the warm-up fetch path wraps page text as untrusted before model use. Its
  SSRF policy is adapted from pinned OpenClaw's reviewed net-policy classifier
  and covers every DNS answer, redirects, mapped/embedded IPv4, NAT64, IPv6
  multicast, RFC 2544 benchmark, and documentation/special-use ranges.

The key-free bundled DuckDuckGo provider was inspected and then invoked through
the real pinned WebSocket gateway. The invocation reached the provider but
failed with `DuckDuckGo returned a bot-detection challenge.` It is therefore
not enabled or claimed. Search remains deferred. Core `web_fetch` was invoked
through the final one-plugin gateway against `https://example.com` and returned
HTTP 200 with `source: core` and `externalContent.untrusted: true,
wrapped: true`.

Final review-fix evidence:

```text
focused Node contract: 42 passed, 0 failed
complete bridge suite: 198 passed, 0 failed
product/static contract: 9 passed
pinned OpenClaw config validate: valid
pinned enabled plugins: personal-operator only
pinned eligible skills: []
pinned live web_fetch: ok=true, source=core, HTTP 200, wrapped=true
pinned low-scope config.patch: rejected, config byte-identical
```

No deployment, push, real credential, private data, or external message was
used. The temporary pinned gateway was stopped cleanly after the proofs.

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
  therefore `minimal` plus the five reviewed `alsoAllow` entries.
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
