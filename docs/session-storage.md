# Session Storage (Public Preview)

AgentCore Runtime supports [Managed Session Storage](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html) — a persistent filesystem at `/mnt/workspace` that survives stop/resume cycles. Data is retained for 14 days of idle time and refreshed on endpoint version updates. Storage is capped at 1 GB per session. See the [launch blog](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/) for details.

## Integration

`scripts/deploy.sh` configures session storage automatically via `filesystemConfigurations` (adding `sessionStorage.mountPath: /mnt/workspace` to the runtime with `update_agent_runtime`). At runtime:

1. **`agentcore-contract.js`** (via `bridge/state-storage.js`) keeps `~/.openclaw` as a real
   directory on the container's local disk, symlinks only `~/.openclaw/workspace` →
   `/mnt/workspace/.openclaw/workspace`, and restores the mount's cold copy of the rest of the
   state dir to local disk before the gateway spawns
2. On resumed sessions (storage has data), S3 restore is skipped
3. On new sessions (storage empty), workspace is restored from S3 once
4. S3 sync switches to **backup mode** (every 30 min vs 5 min) — session storage is primary
5. While the gateway runs, the local state dir is mirrored onto the mount every 5 minutes
   (`STATE_MIRROR_INTERVAL_MS`) and once more on `SIGTERM`, after the gateway has been stopped

If session storage is unavailable, the system falls back to S3 as primary (5 min sync) with no changes needed.

## Why SQLite is not on the mount

Session storage is a loopback NFSv4 export mounted with `local_lock=none`. Plain files work, but
SQLite cannot take even a `RESERVED` lock there, and OpenClaw 2.0 keeps sessions, auth and shared
state in SQLite (`state/openclaw.sqlite`, `agents/<id>/agent/openclaw-agent.sqlite`, …). A 2.0
gateway whose state dir lives on the mount dies at startup with `database is locked`. The state dir
therefore lives on local disk and the mount holds (a) the live agent workspace and (b) a cold mirror
of everything else.

## What Persists

```
~/.openclaw/                     local disk  = OPENCLAW_STATE_DIR (SQLite lives and locks here)
  ├── workspace ──symlink──▶ /mnt/workspace/.openclaw/workspace   (live: memory, user files, AGENTS.md)
  ├── openclaw.json, user-api-keys.json, agents/<id>/…, state/…    (local)
  │        │
  │        └── mirrored every 5 min + on SIGTERM ──▶ /mnt/workspace/.openclaw/<same path>
  │              plain files copied; each *.sqlite written as a consistent snapshot
  │              (node:sqlite online backup API), -wal/-shm never copied
  └── restored from that mirror on the next cold start, before the gateway spawns
                                    │
                              S3 cold backup (every 30 min, same snapshot rule)
```

Workspace files persist live. Everything else persists as of the last mirror: on a graceful stop that
is the moment of shutdown, on an ungraceful one up to `STATE_MIRROR_INTERVAL_MS` (5 min) earlier.
`openclaw.json` and `AGENTS.md` are still regenerated on every init. A 1.x state dir left on the mount
(plain `agents/<id>/sessions/sessions.json`) is restored the same way and then imported by
`openclaw doctor --fix` — see [openclaw-2.0-upgrade.md](openclaw-2.0-upgrade.md). Files the mount
holds from a *previous* layout in which the whole state dir was symlinked are picked up as a mirror.

## Durability and the S3 backup window

Session storage is the primary durability layer: workspace writes land in persistent storage
immediately, and the state mirror on the mount survives a normal stop/resume within the 14-day idle
window.

S3 is only a cold backup. In backup mode it syncs every 30 minutes, and `agentcore-contract.js` performs a final `saveWorkspace()` on `SIGTERM` (see `workspaceSync.cleanup()`). OpenClaw 2.0 keeps session state in per-agent SQLite databases (`agents/<id>/agent/openclaw-agent.sqlite`, WAL mode); `saveWorkspace()` uploads a consistent point-in-time snapshot of each `*.sqlite` file (node:sqlite online backup API) and skips the `-wal`/`-shm` sidecars, so an S3 restore never yields a torn database. See [openclaw-2.0-upgrade.md](openclaw-2.0-upgrade.md). So on a graceful shutdown, S3 is current. On an **unexpected** container stop (no SIGTERM), the S3 backup can lag by up to the 30-minute interval — but session storage still holds the latest state, so this window only matters if session storage is also lost (e.g. the 14-day retention elapses or the endpoint version changes, which refreshes the mount).

## Inspecting the mount directly

Operators can verify what session storage holds for a live session (workspace symlink present, mirror restored, restore skipped, workspace size) without a chat round-trip using `scripts/agentcore-exec.py`, which runs a shell command inside the same microVM via `InvokeAgentRuntimeCommand`. That path runs with the full execution-role credentials and is operator-only — see [docs/execute-command.md](execute-command.md).
