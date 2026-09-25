# Session Storage (Public Preview)

AgentCore Runtime supports [Managed Session Storage](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html) — a persistent filesystem at `/mnt/workspace` that survives stop/resume cycles. Data is retained for 14 days of idle time and refreshed on endpoint version updates. Storage is capped at 1 GB per session. See the [launch blog](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/) for details.

## Integration

`scripts/deploy.sh` configures session storage automatically via `filesystemConfigurations` (adding `sessionStorage.mountPath: /mnt/workspace` to the runtime with `update_agent_runtime`). At runtime:

1. **`agentcore-contract.js`** (via `bridge/state-storage.js`) keeps the whole `~/.openclaw`,
   agent workspace included, as a real directory on the container's local disk and restores the
   mount's mirror of it (`/mnt/workspace/.openclaw`) to local disk before the gateway spawns
2. On resumed sessions (storage has data), the S3 restore fills in only the files that are **missing**
   locally (`restoreWorkspace(namespace, { overwrite: false })`) — files the mount did have are kept.
   The mount's mirror can be partial (a container lost mid-turn had mirrored the workspace within 2 s
   but not the state DBs, whose mirror runs every 5 min), and the state DBs and runtime-skills manifest
   must then come from S3 rather than be recreated empty by the gateway. Kept files are checked against
   S3 without downloading them: every upload stores the sha256 of its bytes as object metadata
   (`x-amz-meta-sha256`), the restore issues one `HeadObject` per kept file, and a kept file whose local
   bytes (a SQLite DB: its snapshot) hash to the stored value is seeded into the upload dedupe so the
   first full save does not re-upload it. A kept file with a different hash, no stored hash (uploaded
   before this was recorded) or a failed `HeadObject` is not seeded and is uploaded by the next save —
   the local copy is never assumed to match S3 without evidence. (Before this, every same-session
   restart re-uploaded the ~9 unchanged small files at the first 30-min save.)
3. On new sessions (storage empty), workspace is restored from S3 once
4. S3 sync switches to **backup mode** — session storage is primary. The full save runs every 30 min
   (vs 5 min), and on top of it a change-driven backup (`workspaceSync.startChangeBackup()`) uploads
   each changed file a few seconds after it changes (5 s debounce, 30 s ceiling per burst, ≤100 files
   per flush, content-hash deduped, `*.sqlite` as snapshots). Changes to **SQLite files only** are
   throttled to one upload per 5 min (`WORKSPACE_SYNC_SQLITE_MIN_INTERVAL_MS`): the gateway writes a
   lease heartbeat into `state/openclaw.sqlite` every 30 s, which would otherwise cost an idle session
   ~116 PUTs (~460 MB) an hour. Any change to a non-SQLite file, the SIGTERM flush and the 30-min full
   save upload the pending snapshots regardless
5. **No upload happens until the S3 restore has completed** (or S3 confirmed there is no saved state
   for the namespace). If the restore fails — listing error, or any object that could not be
   downloaded — uploads stay disabled for the life of that container, so a partial local state never
   overwrites the good copy in S3. Session storage still persists it for a same-session restart
6. While the gateway runs, the local state dir is mirrored onto the mount every 5 minutes
   (`STATE_MIRROR_INTERVAL_MS`) and once more on `SIGTERM`, after the gateway has been stopped;
   the `workspace/` subtree is additionally mirrored within a few seconds of any change by a
   debounced `fs.watch` (2 s debounce, 15 s ceiling per burst)

If session storage is unavailable, the system falls back to S3 as primary (5 min sync) with no changes needed.

## Why the state dir (and the workspace) are not on the mount

Session storage is a loopback NFSv4 export mounted with `local_lock=none`, and it refuses hard links
(`link(2)` fails with `-524`/`ENOTSUPP`). Plain in-place writes work, which is all OpenClaw 1.x did.
OpenClaw 2.0 needs both missing features:

- it keeps sessions, auth and shared state in SQLite (`state/openclaw.sqlite`,
  `agents/<id>/agent/openclaw-agent.sqlite`, …) and cannot take even a `RESERVED` lock on the mount —
  a gateway whose state dir lives there dies at startup with `database is locked`;
- it publishes workspace files atomically with a hard link from a staging file in the same directory
  (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md` on first use, plus other
  workspace artifacts) — a workspace on the mount fails every chat turn with
  `Unknown system error -524, link '.../workspace/openclaw-bootstrap-…/AGENTS.md' -> '.../workspace/AGENTS.md'`.

The whole state dir therefore lives on local disk and the mount holds a mirror of it.

## What Persists

```
~/.openclaw/                     local disk  = OPENCLAW_STATE_DIR (SQLite locks and hard links work here)
  ├── workspace/                 agent workspace: memory, user files, bootstrap files
  │        └── mirrored within seconds of a change (fs.watch, debounced) ──▶ /mnt/workspace/.openclaw/workspace
  ├── openclaw.json, user-api-keys.json, agents/<id>/…, state/…
  │        └── mirrored every 5 min + on SIGTERM ──▶ /mnt/workspace/.openclaw/<same path>
  │              plain files copied (unchanged files skipped); each *.sqlite written as a
  │              consistent snapshot (node:sqlite online backup API), -wal/-shm never copied
  └── everything restored from that mirror on the next cold start, before the gateway spawns
                                    │
                              S3 backup: every changed file ~5 s after it changes (*.sqlite-only changes: at most every 5 min)
                                         + full save every 30 min (same snapshot rule); nothing until the S3 restore has completed
```

Workspace files persist as of the last watcher flush (seconds after the change). Everything else
persists as of the last mirror: on a graceful stop that is the moment of shutdown, on an ungraceful
one up to `STATE_MIRROR_INTERVAL_MS` (5 min) earlier.
`openclaw.json` and `AGENTS.md` are still regenerated on every init. A 1.x state dir left on the mount
(plain `agents/<id>/sessions/sessions.json`) is restored the same way and then imported by
`openclaw doctor --fix` — see [openclaw-2.0-upgrade.md](openclaw-2.0-upgrade.md). Files the mount
holds from a *previous* layout (the whole state dir symlinked onto the mount, or only `workspace/`
symlinked there) are picked up as a mirror; a leftover symlink is replaced by a real directory.

## Durability and the S3 backup window

Session storage is the primary durability layer: workspace writes reach persistent storage within
seconds, and the state mirror on the mount survives a normal stop/resume within the 14-day idle
window.

S3 is the backup that has to be current at any moment, because AgentCore gives the container no usable grace period when it stops a session: on staging the process was gone well under 5 s after `SIGTERM` on an explicit `StopRuntimeSession`, and idle terminations showed no `SIGTERM` at all (see the lifecycle docs: "Termination can last up to 15 seconds due to logging and other process completion" — that is the platform's own teardown, not time given to the container). So `workspace-sync.js` does not wait for a timer or a signal: `startChangeBackup()` watches `~/.openclaw` (`fs.watch`, recursive) and uploads each changed file 5 s after its last change (30 s ceiling per burst, ≤100 files per flush, 4 uploads in parallel). A sha256 index of what was last uploaded skips touched-but-identical files, the same `SKIP_PATTERNS` apply, transient/staging paths (`tmp/`, `openclaw-bootstrap-*`, `.tmp-N`, `*.lock.sqlite`) are ignored, and a change to a `-wal`/`-shm` sidecar marks its main `*.sqlite` dirty, which is then uploaded as a consistent point-in-time snapshot (node:sqlite online backup API — never the raw live file, and only when the snapshot bytes differ from the last upload). OpenClaw 2.0 keeps session state in per-agent SQLite databases (`agents/<id>/agent/openclaw-agent.sqlite`, WAL mode); the same snapshot rule applies to the 30-minute full `saveWorkspace()` (which now also skips unchanged files) and to the final save on `SIGTERM`. On `SIGTERM` the contract first flushes whatever is already dirty (in parallel with stopping the gateway, ≤2 s `GATEWAY_STOP_WAIT_MS`), then sweeps the state dir once more for the gateway's last writes, then mirrors to the mount and runs the full save — best effort, since the container may be gone by then. See [openclaw-2.0-upgrade.md](openclaw-2.0-upgrade.md).

**SQLite-only changes are throttled.** The gateway's 30 s lease heartbeat lands in `state/openclaw.sqlite`, so an idle session would otherwise upload a ~4 MB snapshot every 30 s (~116 PUTs / ~460 MB an hour, measured on staging). When the only dirty files are `*.sqlite`, their snapshots are uploaded at most once every 5 min (`WORKSPACE_SYNC_SQLITE_MIN_INTERVAL_MS`, default 300000); the first SQLite change of a session is not held, a change to any non-SQLite file (memory, workspace, manifest) uploads the pending snapshots with it, and the `SIGTERM` flush and the 30-min full save always include them. An idle session therefore costs ≤ 13 PUTs an hour. **Trade-off:** on a stop with no `SIGTERM` (idle timeout), up to 5 min of SQLite-only history — session rows written with no accompanying memory/workspace write — can be lost. Workspace and memory files are still uploaded within seconds.

**Nothing is uploaded before the restore has completed.** `restoreWorkspace()` gates every upload path (change backup, periodic save, single-file save, `SIGTERM` save): uploads start only once every S3 object has been restored, or S3 confirmed it holds nothing for the namespace (new user). If the listing fails or any object cannot be downloaded, uploads stay disabled in that container — the local state dir may be partial and must not overwrite the S3 copy (on staging a same-session restart with no state DBs had uploaded a fresh empty database over the good one within 20 s). The session-storage mirror keeps running, so a same-session restart still recovers the state; the next new session restores the last good S3 copy.

## Inspecting the mount directly

Operators can verify what session storage holds for a live session (mirror restored, restore skipped, workspace size) without a chat round-trip using `scripts/agentcore-exec.py`, which runs a shell command inside the same microVM via `InvokeAgentRuntimeCommand`. That path runs with the full execution-role credentials and is operator-only — see [docs/execute-command.md](execute-command.md).
