# OpenClaw 2.0 upgrade (2026.3.8 → 2026.9.5)

This deployment now pins **`openclaw@2026.9.5`** (npm `latest`) on **`node:24-slim`**, with
**`clawhub@0.23.3`**, in both `bridge/Dockerfile` and `.bedrock_agentcore/openclaw_agent/Dockerfile`.

"OpenClaw 2.0" is upstream's name for release **v2026.8.1** and everything after it. There is no
`2.x` semver; versions stay date-based. The `extended-stable` channel (`2026.7.35`) is the *pre-2.0*
July line, so adopting 2.0 means tracking the `latest` line. Node 24 is required because 2026.9.3
raised the engine floor to `>=24.16` (Node 22 has a silent `node:sqlite` NUL-truncation bug, and 2.0
keeps session/auth state in SQLite).

## What changed in the bridge

| Area | Before (2026.3.8) | After (2026.9.5) | Why |
|---|---|---|---|
| Base image | `node:22-slim` | `node:24-slim` (both stages) | Engine floor `>=24.16.0` since 2026.9.3 |
| Install | `npm install -g openclaw@2026.3.8 clawhub@0.8.0` | `… openclaw@2026.9.5 clawhub@0.23.3 --allow-scripts=openclaw` | 2.0 ships npm lifecycle scripts; npm 11 warns / npm 12 blocks without the flag |
| WS protocol | `minProtocol: 3, maxProtocol: 3` | `4` / `4` | Gateway requires protocol v4 for operator clients (since 2026.5.12); v3 gets `protocol mismatch`, close 1002 |
| WS identity | `client.id: "openclaw-control-ui"`, `Origin` header set | `client.id: "gateway-client"`, `mode: "backend"`, **no `Origin`** | Control-UI-class clients now need a signed device identity; `gateway-client`/`backend` on loopback token auth is the only device-less path that keeps `operator.*` scopes. A browser-style `Origin` header disables that path |
| Config `tools.exec` | `security: "full", ask: "off"` | `mode: "full"` | `mode` is the canonical exec policy knob; the legacy pair cannot be combined with it |
| Config `gateway.controlUi` | `allowInsecureAuth: true, dangerouslyDisableDeviceAuth: true` | removed | Dead / retired keys. Keeping them makes the startup doctor rewrite `openclaw.json` (+ `.bak` ring) on every boot |
| Startup order | spawn gateway → symlink → write config | state layout + mirror restore → lock cleanup → S3 restore (awaited) → write config → legacy import → spawn gateway | 2.0 validates config strictly and opens its SQLite store at startup; the state dir and config must be complete before spawn |
| State dir | `~/.openclaw` symlinked onto `/mnt/workspace` | `~/.openclaw` on local disk (`OPENCLAW_STATE_DIR`), workspace included; mirrored to the mount every 5 min and on `SIGTERM`, the workspace additionally within seconds of a change (`bridge/state-storage.js`) | Session storage is NFS with `local_lock=none` and no hard links: SQLite cannot lock there (gateway dies with `database is locked`) and 2.0 publishes workspace bootstrap files with `linkSync` (every chat turn fails with `Unknown system error -524`) |
| Shutdown order | S3 save → kill gateway | stop gateway (≤5 s) → snapshot state dir to the mount → S3 save | A closed database gives a fully quiesced snapshot for the next cold start |
| Skills | bare `clawhub install <slug>` | qualified `@owner/slug --version` for slugs ClawHub now hosts twice; flattened to `/skills/<slug>`; a missing skill fails the build | clawhub ≥ 0.23 refuses an ambiguous bare slug and the old retry loop shipped 2/5 skills silently |
| Legacy sessions | n/a | `openclaw doctor --fix --non-interactive` runs once when `agents/<id>/sessions/sessions.json` exists | 2.0 does not migrate on its own: a legacy store makes the gateway **refuse readiness** |
| S3 sync | copies every file | skips `*.sqlite-wal`/`-shm`/`-journal`, uploads a consistent **snapshot** of each `*.sqlite` | Copying a live WAL-mode database file-by-file yields torn/rolled-back restores |

Verified unchanged: the Bedrock proxy wire contract (`openai-completions` to a custom `baseUrl`),
`scoped-credentials.js` / `credential_process`, the skills format under `/skills`, the
`chat.send` params, `sessionKey: "global"`, and the Lambda router's marker parsing.

## User-visible changes in 2.0 and the knobs that preserve the old behaviour

OpenClaw 2.0 changed several defaults. `writeOpenClawConfig()` in `bridge/agentcore-contract.js`
pins each one back to the 2026.3.8 behaviour so upgraded users see no difference. Remove or change
the knob if you want the new behaviour.

| # | 2.0 default | Effect if left at default | Knob in `openclaw.json` (as generated) |
|---|---|---|---|
| 1 | Conversations no longer reset daily ("keep conversations across idle periods and day boundaries") | Context persists as long as session storage does; long-running threads grow until compaction | `session.reset: { mode: "daily", atHour: 4 }` |
| 2 | New built-in tools exposed by `tools.profile: "full"`: `terminal`, `process`, `plugins`, `ask_user`, `secrets`, `screen`, `progress_card`, `nodes`, `heartbeat_respond`, `image_generate`, `music_generate`, `video_generate`, `tts` | The model may call tools that need a Control UI or a human answering a masked prompt (there is none), or media generation not routed via the proxy | added to `tools.deny` |
| 3 | Autonomous self-learning (Skill Workshop) on by default | Extra model calls after runs; new files under `agents/main/agent/workshop-skills` (synced to S3) | `skills.workshop.autonomous.mode: "off"` |
| 4 | Active Memory cross-conversation recall on for "personal installs"; grounded dreaming (background memory consolidation) on | A retrieval model pass before replies and scheduled consolidation runs — both Bedrock spend | `memory.search.rememberAcrossConversations: false`, `plugins.entries["active-memory"].enabled: false`, `plugins.entries["memory-core"].config.dreaming.enabled: false` |

Also worth knowing (no knob needed): during gateway startup 2.0 may answer `connect` with
`UNAVAILABLE "gateway starting; retry shortly"`. The bridge surfaces any non-ok `connect` as
`Auth failed: …`, so a user who hits that window sees that text instead of the previous WebSocket
timeout fallback. The lightweight agent still handles messages until the gateway is ready.

## Upgrade path for existing deployments

1. Build and push the new bridge image, bump `image_version` in `cdk.json`, redeploy
   `OpenClawAgentCore` (see README "Deploy new bridge version").
2. **First boot per user — legacy session import.** Users who chatted on 2026.3.8 have
   `~/.openclaw/agents/main/sessions/sessions.json` (+ `.jsonl` transcripts) on their session-storage
   mount or in their S3 backup. 2.0 keeps session rows in
   `~/.openclaw/agents/main/agent/openclaw-agent.sqlite` and **refuses readiness** while a legacy
   store is present ("prints the Doctor command instead of serving empty history"). `init()` handles
   this: after the S3 restore and config write, and before the gateway spawns, it runs
   `openclaw doctor --fix --non-interactive` (bounded by `OPENCLAW_MIGRATION_TIMEOUT_MS`, default
   180 s) with the same scoped environment as the gateway. Successful import removes
   `sessions.json`; history carries over. Logs appear as `[openclaw:doctor] …` and
   `[contract] Legacy session store imported into SQLite in Nms`.
3. **If the import fails** (exit ≠ 0, timeout, or `sessions.json` still present), the leftover index
   is moved aside to `sessions.json.pre-2.0-unreadable-<timestamp>` and the gateway starts with
   empty history for that agent. Transcripts (`.jsonl`) are left in place, so an operator can retry
   later with `openclaw doctor --session-sqlite import --session-sqlite-all-agents` after restoring
   the index. The lightweight agent covers the user during the migration window as during any
   cold start.
4. **Risk to be aware of (R2):** a `sessions.json` that the pre-2.0 periodic S3 sync uploaded
   mid-write (or one skipped by the 10 MB per-file cap — such files are *not* restored at all, so
   only the transcripts come back) is exactly the "unreadable legacy index" case. Without step 3 the
   gateway would never become ready for that user; with it, they lose the pre-2.0 session index but
   keep the transcript files.
5. Session storage is unaffected by the upgrade itself, but note that a new image version refreshes
   the `/mnt/workspace` mount (see `docs/session-storage.md`), so the first 2.0 boot for each user
   is normally an S3 restore followed by the import above.

## Validating a build

- `docker buildx build --platform linux/arm64 -f bridge/Dockerfile .` — settles the native
  prebuilds (`koffi`, `@lydell/node-pty`, `tree-sitter-bash` on arm64) and `node:sqlite` on Node 24.
- `cd bridge && node --test` (Node 24) — includes the SQLite snapshot tests in
  `workspace-sync.test.js` and the state-dir relocate/mirror/restore tests in
  `state-storage.test.js`, which skip on runtimes without `node:sqlite`.
- Inside the image: `openclaw config validate` against a generated `openclaw.json` catches any key
  the strict 2.0 schema rejects.

Things only a deployed container settles: whether AgentCore's loopback is classified `direct_local`
(otherwise `chat.send` fails with a missing-scope error — fallback is a signed device identity),
whether `sessionKey: "global"` still lands on agent `main`, gateway boot time versus the e2e
and `_OPENCLAW_STARTUP_TIMEOUT_S`. The filesystem question is settled: `/mnt/workspace` is
`nfs4` with `local_lock=none`, on which SQLite cannot lock at all — hence the local state dir above.
