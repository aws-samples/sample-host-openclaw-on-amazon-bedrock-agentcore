# Session Storage (Public Preview)

AgentCore Runtime supports [Managed Session Storage](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html) — a persistent filesystem at `/mnt/workspace` that survives stop/resume cycles. Data is retained for 14 days of idle time and refreshed on endpoint version updates. See the [launch blog](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/) for details.

## Integration

`scripts/deploy.sh` configures session storage automatically via `filesystemConfigurations`. At runtime:

1. **`agentcore-contract.js`** symlinks `~/.openclaw` → `/mnt/workspace/.openclaw`
2. On resumed sessions (storage has data), S3 restore is skipped
3. On new sessions (storage empty), workspace is restored from S3 once
4. S3 sync switches to **backup mode** (every 30 min vs 5 min) — session storage is primary

If session storage is unavailable, the system falls back to S3 as primary with no changes needed.

## What Persists

Session storage preserves the entire `~/.openclaw/` directory, including conversation history, cron jobs, memory, credentials, caches, and logs. Two files are regenerated on every init regardless: `openclaw.json` (gateway config) and `AGENTS.md` (agent instructions tied to the container version).

Notably, session storage also retains files that S3 backup skips (caches, `node_modules/`, logs, media) — so resumed sessions are faster than cold S3 restores.

```
~/.openclaw  ──symlink──▶  /mnt/workspace/.openclaw  (persistent)
                                    │
                              S3 cold backup (every 30 min)
```
