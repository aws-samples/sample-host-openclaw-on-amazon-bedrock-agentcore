# Task 6 — Exact-target public URL reader

Single-writer worktree: `/private/tmp/personal-operator-v1-task6-web-reader`
Branch: `codex/po-v1-task6-web-reader` (from Gate A `5e4930e`).
Python: `/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python`
Node 24: `PATH="/opt/homebrew/opt/node@24/bin:$PATH"`.

## Summary

Added a gateway-mediated, offline-by-default exact-target public URL reader.
Grants are derived ONLY from exact https URLs literally present in the current
authenticated message; the fetch pipeline pins DNS, connects only to the pinned
public IP, is GET-only with no cookies/auth headers, bounds redirects to the
same host, enforces MIME/size/time limits, sanitizes page-instruction injection,
and marks output untrusted. Every denial makes zero network calls on the
injected fakes. The runtime's old in-process web helper stack was removed so the
lightweight runtime holds no direct network authority; `po_web_read` reaches the
system only through the capability relay adapter.

## Commands

- `<py> -m pytest -q lambda/capabilities` -> 213 passed
- `<py> -m pytest -q lambda/capabilities/test_web_reader.py` -> 34 passed
- `node --test bridge/lightweight-agent.test.js` -> 13 passed / 0 failed
- `git diff --check 5e4930e..HEAD` -> clean (exit 0)

Pre-existing environmental failures (missing `bridge/node_modules`, i.e.
`@aws-sdk/*` / `ipaddr.js`) in `capability-catalog.test.js` (4 pass / 2 fail) and
`capability-relay.test.js` (10 pass / 5 fail) are identical before and after this
change and are unrelated to Task 6.

## Files

Create:
- `lambda/capabilities/web_reader.py` — `WebReadAdapter` + `build_web_read_adapter`
- `lambda/capabilities/target_grants.py` — `derive_target_grants` (+ projectors)
- `lambda/capabilities/test_web_reader.py` — hostile corpus (34 tests)

Modify:
- `lambda/capabilities/gateway.py` — documented single wiring point
  (`build_web_read_adapter`, `WEB_READ_OPERATION_ID`)
- `lambda/capabilities/contracts.py` — factored shared public-IP predicate
  (`_ip_is_globally_routable`, `public_ip_or_none`) reused by URL gate + reader
- `bridge/lightweight-agent.js` — removed dead web fetch/DNS/TLS/sanitizer stack,
  dropped `node:dns/https/net` + `web-network-policy` + openclaw security-runtime
  requires, and dropped removed symbols from `module.exports`
- `bridge/lightweight-agent.test.js` — assert removal + gateway-only reachability

## Hostile cases covered

target modification (mismatch + same-tool-use argument mutation), previous-turn
URL (no grant + request-id-bound hash), workspace-derived URL (no grant),
private/special/link-local/metadata IP + non-https at mint, resolver metadata IP
denied before connect, mixed public+private DNS answer, empty DNS answer, DNS
rebinding (connect only to pinned IP; redirect re-resolve to private denied),
encoded redirect Location, changed host across redirect, NO_REDIRECT any-3xx,
bad MIME, size overflow, time overflow, page-instruction exfiltration sanitized
+ untrusted provenance, GET-only no-cookie/no-auth headers, redirect fresh
minimal headers, adapter disabled by default, production composition never wires
a network seam, log/content retention, and zero-network meta across denials.

## Deviations

- `bridge/plugins/personal-operator/index.js` was in the planned modify set but
  needed no change: `po_web_read` is already a generic relay-routed capability
  tool with no web branch and no residual web import. Confirmed only; left
  untouched.
- Preferred the offline-purity option from the plan: production composition
  keeps `adapters={}`; the reader is exposed via the documented
  `build_web_read_adapter` factory that only tests (and a future Linux-isolated
  fetch path) pass into `CapabilityGateway(adapters=...)`.
- `bridge/web-network-policy.js` is now orphaned (no importer) but is outside the
  declared file scope, so it was left in place.
