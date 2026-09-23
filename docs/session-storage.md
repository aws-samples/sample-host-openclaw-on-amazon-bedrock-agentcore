# Session Storage (Public Preview)

AgentCore Runtime supports [Managed Session Storage](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html) — a persistent filesystem at `/mnt/workspace` that survives stop/resume cycles. Data is retained for 14 days of idle time and refreshed on endpoint version updates. Storage is capped at 1 GB per session. See the [launch blog](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/) for details.

## Integration

`scripts/deploy.sh` configures session storage automatically via `filesystemConfigurations` (adding `sessionStorage.mountPath: /mnt/workspace` to the runtime with `update_agent_runtime`). At runtime:

1. **`agentcore-contract.js`** symlinks `~/.openclaw` → `/mnt/workspace/.openclaw`
2. On resumed sessions (storage has data), S3 restore is skipped
3. On new sessions (storage empty), workspace is restored from S3 once
4. S3 sync switches to **backup mode** (every 30 min vs 5 min) — session storage is primary

If session storage is unavailable, the system falls back to S3 as primary (5 min sync) with no changes needed.

## What Persists

Session storage preserves the entire `~/.openclaw/` directory, including conversation history, cron jobs, memory, credentials, caches, and logs. Two files are regenerated on every init regardless: `openclaw.json` (gateway config) and `AGENTS.md` (agent instructions tied to the container version).

Notably, session storage also retains files that S3 backup skips (caches, `node_modules/`, logs, media) — so resumed sessions are faster than cold S3 restores.

```
~/.openclaw  ──symlink──▶  /mnt/workspace/.openclaw  (persistent)
                                    │
                              S3 cold backup (every 30 min)
```

## Durability and the S3 backup window

Session storage is the primary durability layer: because `~/.openclaw` is a symlink into `/mnt/workspace`, writes land in persistent storage immediately and survive a normal stop/resume within the 14-day idle window.

S3 is only a cold backup. In backup mode it syncs every 30 minutes, and `agentcore-contract.js` performs a final `saveWorkspace()` on `SIGTERM` (see `workspaceSync.cleanup()`). So on a graceful shutdown, S3 is current. On an **unexpected** container stop (no SIGTERM), the S3 backup can lag by up to the 30-minute interval — but session storage still holds the latest state, so this window only matters if session storage is also lost (e.g. the 14-day retention elapses or the endpoint version changes, which refreshes the mount).
