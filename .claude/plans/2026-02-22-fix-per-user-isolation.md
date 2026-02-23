# Fix Per-User File Isolation — Investigation & Remediation Plan

## Problem Statement

A Slack user can see the bot name that was set by a different Telegram user. The per-user file isolation implemented in the previous session is not fully effective.

## Root Cause Analysis

After thorough investigation, there are **3 compounding issues**:

### Issue 1: Identity extraction depends on message envelope format (HIGH RISK)

The proxy's `extractSessionMetadata()` (agentcore-proxy.js:46-125) expects OpenClaw to wrap user messages in an envelope format:
```
[Telegram John Doe id:12345 Fri 2026-02-21 10:30:45] Hello
```

**If OpenClaw doesn't add this envelope** (or uses a different format), the regex at line 67-68 never matches, and ALL users fall back to `default-user`. This means every user reads/writes to the same `default_user/` S3 prefix — **isolation completely broken**.

We **cannot verify this from CloudWatch** because container stdout/stderr doesn't appear in the available log groups. The identity-diagnostic logging was removed during code review (CRITICAL-1 fix).

**Action:** Add structured diagnostic logging back (safe version — no message content) and add a temporary diagnostic endpoint to test identity extraction.

### Issue 2: `tools.profile: "full"` still exposes built-in file tools (MEDIUM RISK)

Even if S3 isolation works perfectly, OpenClaw's `tools.profile: "full"` gives Claude built-in tools like `write_file`, `read_file`, `append_file` that operate on the **shared local filesystem**. The system prompt tells Claude "NEVER write to local files", but this is instruction-based only — Claude can still choose to use them.

If Claude writes to `/root/.openclaw/MEMORY.md` or `/root/.openclaw/IDENTITY.md` using built-in tools instead of the s3-user-files skill, all users share those files.

**Action:** Restrict tools profile to exclude file write/read, or use a custom tools configuration.

### Issue 3: Pre-existing shared files on disk (LOW RISK)

Before this fix was deployed, Claude was writing IDENTITY.md, MEMORY.md etc. to the shared workspace. The old container image had these files. The new image (v11) starts fresh, but if Claude reads local files first (before trying S3), it could find stale shared data.

**Action:** Ensure the system prompt priority is clear — S3 first, never local files.

---

## Implementation Plan

### Phase 1: Add Diagnostic Logging (must do first)

**File:** `bridge/agentcore-proxy.js`

1. After `extractSessionMetadata()` returns (line 719), add a **safe** log line:
   ```javascript
   console.log(`[proxy] Identity: actorId=${actorId}, channel=${channel}, sessionId=${sessionId.slice(0,12)}...`);
   ```
   This logs the resolved identity WITHOUT leaking message content.

2. Add a log line showing which extraction method succeeded (header, user field, envelope, name, or fallback). Add a `source` field to the return value of `extractSessionMetadata()`.

3. Log the raw first user message's first 80 chars (envelope prefix only, not full content) to debug whether OpenClaw sends the expected format:
   ```javascript
   const firstUserMsg = messages.find(m => m.role === "user");
   if (firstUserMsg && typeof firstUserMsg.content === "string") {
     console.log(`[proxy] First user msg prefix: "${firstUserMsg.content.slice(0, 80)}"`);
   }
   ```

### Phase 2: Restrict Built-in File Tools

**File:** `bridge/entrypoint.sh`

Change `tools.profile` from `"full"` to a custom configuration that excludes file write tools. OpenClaw supports tool profiles and selective disabling.

**Option A (preferred):** Use `"profile": "full"` but add a `"disabled"` list:
```json
"tools": {
  "profile": "full",
  "disabled": ["write_file", "append_file"]
}
```

**Option B:** If OpenClaw doesn't support `disabled`, switch to `"profile": "standard"` or define explicit tool includes.

**Option C:** If neither works, add stronger system prompt language and rely on instruction-following. This is the weakest option.

**Research needed:** Check OpenClaw docs/source for how to disable specific built-in tools while keeping others.

### Phase 3: Deploy & Verify with Live Logs

1. Build and push new Docker image (v12)
2. Deploy via CDK
3. Stop old runtime session
4. Wait for new session to start
5. Send a test message from Telegram
6. Check CloudWatch logs for identity extraction results
7. Verify the actorId is `telegram:XXXXX` (not `default-user`)

### Phase 4: Fix Based on Diagnostic Results

**If envelope parsing works** (actorId shows `telegram:XXXXX`):
- The issue is Claude ignoring system prompt and using built-in file tools
- Fix: Restrict built-in tools (Phase 2)

**If envelope parsing fails** (actorId shows `default-user`):
- OpenClaw doesn't send the expected envelope format
- Fix: Change identity extraction to use a different method:
  - Option A: Set `x-openclaw-actor-id` header in OpenClaw config (if supported)
  - Option B: Parse the actual format OpenClaw uses (based on log output)
  - Option C: Use OpenClaw's `user` field in the API request body

### Phase 5: End-to-End Verification

1. From Telegram: "My name is TelegramUser, remember it"
2. Check S3: `aws s3 ls s3://openclaw-user-files-657117630614-ap-southeast-2/telegram_XXXXX/`
3. From Slack: "What's my name?"
4. Verify Slack user does NOT see "TelegramUser"
5. Check S3: verify separate namespace for Slack user

---

## Files to Modify

| File | Change | Phase |
|------|--------|-------|
| `bridge/agentcore-proxy.js` | Add diagnostic logging, identity source tracking | 1 |
| `bridge/entrypoint.sh` | Restrict built-in file tools | 2 |
| `bridge/agentcore-proxy.js` | Fix identity extraction based on diagnostics | 4 |

## Risks

- **Diagnostic logging**: Minimal risk — only logs actorId and message prefix (no full content)
- **Restricting tools**: Medium risk — could break other Claude functionality if we disable too aggressively
- **Identity extraction changes**: Low risk — fallback to `default-user` is safe (just loses isolation)

## Success Criteria

- CloudWatch logs show distinct actorIds per user (e.g., `telegram:12345`, `slack:U0123456789`)
- S3 bucket shows separate namespaces per user
- Telegram user's bot name is NOT visible to Slack user
- No regression in chat functionality
