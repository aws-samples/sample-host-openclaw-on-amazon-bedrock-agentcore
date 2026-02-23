# TODO: Per-User Memory Isolation Fix — Verification Steps

## Code Changes (DONE)

- [x] Enhanced `extractSessionMetadata()` with envelope parsing (priority 3) and message `name` fallback (priority 4)
- [x] Added diagnostic logging (`[proxy][identity-diag]`) after `JSON.parse(body)` in POST handler
- [x] Added `console.warn` when falling back to `default-user`
- [x] Updated CLAUDE.md with new identity resolution documentation

## Envelope Regex Fix (DONE)

**Root cause of Telegram identity issue**: The original regex expected `[Telegram telegram:12345 ...]` format, but OpenClaw's actual envelope is `[Telegram John Doe (@johndoe) id:12345 ...]`. The `from` field is the display name, not `channel:userId`. The `id:` is embedded later in the header.

**Fix**: Changed regex from `/^\[(?:Slack|Telegram|Discord|WhatsApp)\s+(\w+:\S+)/i` to `/^\[(Slack|Telegram|Discord|WhatsApp)\s+[^\]]*?\bid:(\S+)/i`. This extracts the channel name (group 1) and the id value after `id:` (group 2), then combines them as `channel:id` (e.g., `telegram:12345`).

Tested against all envelope formats:
- `[Telegram John Doe (@johndoe) id:12345 ...]` -> `telegram:12345`
- `[Slack Alice id:U0123456789 ...]` -> `slack:U0123456789`
- `[Telegram id:12345 ...]` (no name) -> `telegram:12345`
- `[Telegram My Group id:-1001234567890 ...]` -> `telegram:-1001234567890`
- No envelope / no `id:` -> falls through correctly

## Deployment (DONE)

- [x] Build and push updated container image (IMAGE_VERSION=10)
- [x] `cdk deploy OpenClawAgentCore` — runtime AND endpoint both updated
- [x] Stopped old session, started fresh session with new image
- [x] Keepalive verified: `proxy_ready: true`, `memory: configured`

## AccessDeniedException Fix (DONE)

**Root cause**: Runtime endpoint version drift. Added `agent_runtime_version=self.runtime.attr_agent_runtime_version` to `CfnRuntimeEndpoint`.

## Verification (READY — runtime is up, send messages to test)

- [ ] Test with 2+ different users on the **same channel** (e.g., two Slack users)
- [ ] Check CloudWatch logs for `[proxy][identity-diag]` entries — confirm `actorId` is `telegram:NNNNN` or `slack:UXXXXXX`
- [ ] Check logs for `[proxy] Memory retrieval:` — verify distinct `actorId` per user
- [ ] Confirm no `[proxy] WARNING: No user identity` messages appear after fix
- [ ] Cross-channel test: send from Telegram user A and Slack user B — verify distinct namespaces
- [ ] Memory isolation test: have user A tell bot a secret, verify user B does not see it in memory context

Note: Telegram shows `connected: false` in keepalive status but user reports it IS working — status reporting may be inaccurate.

## Cleanup (after verification)

- [ ] Remove `[proxy][identity-diag]` diagnostic logging from `agentcore-proxy.js` after confirming fix
