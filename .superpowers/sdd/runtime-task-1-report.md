# Runtime Hardening Task 1 implementation report

## RH1 bounded-admission and deterministic-image closure (2026-07-18)

The Wave 4 review found two remaining retention/idempotency gaps. The trusted
invocation registry could retain unlimited unique in-flight request closures,
and the hand-written gateway queue could retain unlimited pending messages.
Image retries also used timestamp/random object names, so the same platform
event produced a different canonical request hash on every delivery.

The runtime now enforces both admission bounds before work can multiply:

- at most eight unique trusted invocation IDs may be in flight; a ninth new ID
  fails closed with typed `RUNTIME_OVERLOADED` and cannot escape through the
  fallback executor;
- an existing same-ID/same-request retry remains admissible at capacity and
  shares the original promise, while same-ID/different-work still conflicts;
- the unbounded message array is replaced by a serial executor with exactly
  one running gateway call and at most seven retained pending tasks; excess
  work returns the same typed fail-closed overload;
- in-flight and originating uncertain registry entries remain non-evictable;
  ordinary settled outcomes are retained for at most one hour and capped at 64
  entries (at the router's 500 KB response ceiling, at most about 32 MB of
  response text).

The router now derives and validates the trusted invocation ID before image
download/upload. Image filenames are deterministic from that stable event ID
and the SHA-256 of the exact downloaded bytes. An identical platform delivery
therefore overwrites the same S3 object and sends the same structured request
to the runtime; a changed event ID or changed image bytes produces different
work, and reusing the old event ID with changed work is rejected by the runtime
request-hash binding. This wave intentionally leaves the key under the current
effective actor namespace. RH2 must migrate router, proxy, credentials, and
runtime namespace atomically rather than introducing a split namespace here.

RED evidence was captured first: the focused Node contract admitted a third
unique in-flight ID, lacked the bounded executor, and still contained the
unbounded production queue (3 failures); the image contract lacked the stable
upload identity and handler propagation (8 failures). A handler-level Slack
platform-event test now processes an identical image event twice with the real
key builder and mocked S3, proving equal S3 keys, invocation IDs, and structured
runtime work; changed event/image input differs.

Fresh Wave 4 evidence:

```text
focused production state machine/admission contracts: 30 passed, 0 failed
focused Telegram/Slack/Feishu image contracts: 66 passed, 0 failed
complete bridge suite: 232 passed, 0 failed
complete router suite: 155 passed; 1 documented pre-existing malformed-content parser failure
product/static contract: 9 passed, 0 failed
JavaScript syntax, Python byte-compile, and git diff checks: passed
```

The unrelated router baseline failure is
`test_formatting_integration.py::TestFullPipeline::test_regex_fallback_for_malformed_json`;
the Wave 4 diff does not touch that parser. Per review direction it remains out
of this security commit and must close before release. No deploy, push, real
credential, private data, or external message was used.

## RH1 uncertain-run and duplicate-delivery closure (2026-07-18)

The accepted-run review found a commit ambiguity in the bridge: streamed
deltas and terminal output shared one buffer, and transport failure after an
accepted run could return partial text or select the lightweight executor. The
production route is now an explicit fail-closed state machine:

- only a same-run terminal `status:"ok"` response commits output; stream
  deltas are presentation-only and terminal `error` / `timeout` responses are
  typed failures;
- sending the `agent` request is the uncertainty boundary, because a transport
  failure can hide the accepted acknowledgement after provider work starts;
- while the owning socket remains open, timeout/error first sends exact-run
  `chat.abort`, then requires same-run `agent.wait` evidence with terminal
  `error` / `timeout`, `stopReason:"rpc"`, and `endedAt` before considering the
  run contained;
- reconciliation is hard-capped at five seconds, below pinned OpenClaw's
  15-second synthetic cancellation-snapshot grace. A close/lost socket or any
  unproven settlement becomes `UNCERTAIN_AGENT_RUN`;
- uncertainty permanently quarantines the process, terminates the OpenClaw
  child with `SIGTERM` then bounded `SIGKILL`, blocks scheduled restart, and
  rejects later work without invoking either executor.

The router now derives a fixed `po1_<sha256>` invocation identity from the
channel, immutable platform event ID, and internal user ID. Before any identity
write or initialization, the runtime binds that ID to a canonical request hash
covering actor, channel, and either exact text or normalized image references.
A process-lifetime registry single-flights concurrent duplicates across both
OpenClaw and the lightweight executor, rejects same-ID/different-work conflicts,
caches typed outcomes, never evicts in-flight or originating uncertain entries,
and bounds ordinary settled entries to one hour / 64 entries. Therefore a
retry cannot switch executors merely because readiness changed.

This is deliberately not a durable exactly-once claim. Pinned OpenClaw's own
RPC dedupe is in-memory, five minutes / 1,000 entries, and disappears on
restart. The runtime registry also disappears with its process. Runtime
Hardening Task 4 still needs the durable router update ledger for late retries
and cross-runtime replay suppression.

RED evidence captured during this closure included missing state-machine APIs,
accepted-delta close/error/timeout paths returning ambiguously, dispatch-before-
ack transport failure classified as retryable, missing persistent quarantine
and delayed-restart guards, abort acknowledgement accepted without terminal
reconciliation, no stable trusted run identity, no cross-executor registry,
same-ID/different-image conflicts, and caller-expandable reconciliation beyond
the pinned synthetic-snapshot window.

Fresh local evidence:

```text
focused production state machine and registry: 28 passed, 0 failed
complete bridge suite: 230 passed, 0 failed
Telegram/Slack/Feishu router contracts: 64 passed
product/static contract: 9 passed
pinned source commit: 4bfaccafd62ac2ff2e70ca1decc40fb1297ab438
pinned exact abort run: dcd57cd3-38c0-4af0-860c-233199846351
pinned chat.abort: ok=true, aborted=true, runIds contained the exact run
pinned agent.wait: status=error, stopReason=rpc, finite startedAt/endedAt
pinned exact-run abort/reconciliation assertion: true
temporary gateway and proof-model ports 18889/18890: not listening
```

The live proof used only a loopback dummy provider and the least-privilege
`operator.read` / `operator.write` connection. No admin/backend/OpenResponses
route, patch, deploy, push, real credential, private data, or external message
was used.

## RH1 final invocation-boundary closure (2026-07-18)

The final review found that pinned OpenClaw 2026.7.2 clears self-declared
scopes for an Origin-bearing, device-less shared-token backend connection. It
also reserves `chat.send.suppressCommandInterpretation` for
`operator.admin`. Granting admin would defeat the reviewed capability
boundary, so the runtime now uses the pinned `agent` RPC instead:

- the loopback WebSocket omits `Origin` and identifies as `client.id="cli"`,
  `mode="cli"` on protocol 4;
- the client requests only `operator.read` and `operator.write`, then derives
  and exact-matches the scopes in the server's hello before sending work;
- every run has only `{message, sessionKey:"global", deliver:false,
  idempotencyKey}` and uses one value for request, idempotency, and run ID;
- the bridge ignores unrelated IDs and accepted/in-flight responses, then
  resolves only the correlated terminal response and extracts
  `result.payloads[].text`;
- `/new` and `/reset` fail closed because they require admin. Other
  slash-prefixed text follows the ordinary agent/model route. The configured
  `commands.text=false` remains defense in depth and is not claimed as the
  guarantee for this route.

The warm-up retrieval boundary also now uses typed `{ok, content}` / `{ok:
false, error}` results, so a successful page beginning with `Error:` cannot
escape wrapping. In the image module graph it imports pinned OpenClaw's public
`plugin-sdk/security-runtime` wrapper; source-only tests exercise an equivalent
commit-pinned fallback. Both use a random 16-hex boundary ID, sanitize injected
start/end markers (including homoglyph/zero-width variants), and remove model
special tokens.

Fresh final evidence:

```text
focused invocation/wrapper/policy contracts: 33 passed, 0 failed
complete bridge suite: 211 passed, 0 failed
product/static contract: 9 passed
pinned hello scopes: operator.read, operator.write (server reported)
pinned config.patch: rejected missing operator.admin; config SHA-256 unchanged
pinned /status: provider saw literal input; accepted then terminal ok
pinned /config set commands.text true: provider saw literal input; terminal ok
pinned /po_file_read notes.md: provider saw literal input; terminal ok
pinned /new and /reset: rejected missing operator.admin; zero provider calls
pinned session continuity: one session ID across all successful model turns
pinned duplicate run ID: cached terminal result; zero extra provider calls
pinned public security export: loaded; forged close marker sanitized
temporary gateway and model-proof server: stopped; proof ports not listening
```

No OpenClaw patch, admin scope, deployment, push, real credential, private
data, or external message was used.

## RH1 review-fix closure (2026-07-17)

The review-fix wave closes the effective-capability gaps left by the initial
Task 1 implementation:

- `agents.defaults.skills` and `skills.allowBundled` are both empty. The exact
  pinned CLI returned an empty eligible skill inventory, so no skill text is
  model-visible.
- `commands.text=false` is configured as defense in depth. The final
  invocation-boundary section above supersedes this wave's former backend
  route and records the mechanism that actually keeps slash-prefixed text from
  becoming a privileged gateway command.
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
