---
name: s3-memory-isolation
description: "Pattern for adding new per-user S3-backed memory types to OpenClaw (identity, preferences, tasks, etc.)"
user-invocable: true
---

# S3 Per-User Memory Isolation Pattern

Use this pattern when adding a new type of persistent per-user data to OpenClaw. The workspace pre-loading system already handles 5 bootstrap files; this pattern covers adding additional file types.

## Architecture (3 layers)

```
Layer 1: Proxy (agentcore-proxy.js)    — pre-loads workspace files into system prompt
Layer 2: Skill scripts (s3-user-files) — LLM-callable CRUD tools
Layer 3: System prompt injection       — tells LLM what data exists and how to update it
```

## Current Workspace (pre-loaded every request)

The proxy pre-loads these 5 files in parallel via `WORKSPACE_FILES` array:

| File | Label | Purpose | Priority |
|------|-------|---------|----------|
| `AGENTS.md` | Operating Instructions | rules, priorities, and behavioral guidelines | 1 (highest) |
| `SOUL.md` | Agent Persona | persona, tone, and communication boundaries | 2 |
| `USER.md` | User Preferences | user identity and communication preferences | 3 |
| `IDENTITY.md` | Agent Identity | agent name, vibe, and emoji | 4 |
| `TOOLS.md` | Tools Documentation | local tools and conventions documentation | 5 (lowest) |

Optional files (not pre-loaded, available via `read_user_file`):
- `MEMORY.md` — freeform notes and memories
- `HEARTBEAT.md` — scheduled check-in preferences

## Step-by-step: Add a New Pre-loaded File

### 1. Add to WORKSPACE_FILES array

In `bridge/agentcore-proxy.js`, add an entry to the `WORKSPACE_FILES` constant:

```javascript
const WORKSPACE_FILES = [
  // ... existing entries ...
  { filename: "NEWFILE.md", label: "Display Label", purpose: "what this file stores" },
];
```

The file is automatically:
- Read from S3 in parallel with other workspace files
- Sanitized (backtick escape + 4096-char per-file cap)
- Rendered as `## Workspace: Display Label (NEWFILE.md)` section
- Shown as "not yet created" with save instructions when missing
- Subject to 20,000-char total workspace cap (lower-priority files skipped first)

### 2. No manual prompt injection needed

The `buildUserIdentityContext()` function iterates `WORKSPACE_FILES` automatically. Adding to the array is sufficient.

### 3. Security checklist (MANDATORY for every new file type)

- [ ] **Sanitize content**: Handled by `sanitizeWorkspaceContent()` — `.slice(0, 4096).replace(/```/g, "~~~")`
- [ ] **Validate namespace**: Use existing `VALID_ACTOR_ID` regex — never trust user-provided IDs
- [ ] **Graceful degradation**: `readUserFileFromS3` wraps in try/catch, returns `""` on failure
- [ ] **No PII in logs**: Log byte count only, never content
- [ ] **Use cached S3 client**: `getS3Client()` not `new S3Client()` per request

### 4. The LLM already has CRUD tools

No new skill scripts needed — `s3-user-files` already provides:
- `read_user_file <namespace> <filename>` — works for any filename
- `write_user_file <namespace> <filename> <content>` — works for any filename
- `list_user_files <namespace>` — shows all files
- `delete_user_file <namespace> <filename>`

### 5. Add tests

Mirror new pre-load behavior in `bridge/proxy-identity.test.js`. The `buildIdentityText` helper accepts a `workspaceContents` object (`{ "FILENAME.md": "content", ... }`):

```javascript
it("includes new file when content provided", () => {
  const result = buildIdentityText("slack:U0AGD41CBGS", "slack", {
    "NEWFILE.md": "# Content\nSome data",
  });
  assert.ok(result.includes("Workspace: Display Label (NEWFILE.md)"));
  assert.ok(result.includes("Some data"));
});
```

## Key constraints

- **S3 key**: `{namespace}/{filename}` where `namespace = actorId.replace(/:/g, "_")`
- **Namespace validation**: `/^(telegram|slack|discord|whatsapp)_[a-zA-Z0-9_-]{1,64}$/`
- **Per-file cap**: 4096 chars — larger files truncated via `sanitizeWorkspaceContent()`
- **Total cap**: 20,000 chars across all workspace files — lower-priority files skipped with `read_user_file` fallback
- **Bucket**: `S3_USER_FILES_BUCKET` env var, encrypted with project CMK
- **Region**: `AWS_REGION` env var (fail-fast, no fallback)
- **Pre-load is read-only**: Proxy reads at request time; writes only happen via LLM tool calls
- **Namespace is immutable**: System-determined from channel identity, never user-modifiable

## Anti-patterns

| Don't | Why | Instead |
|-------|-----|---------|
| Let LLM read workspace files via tool calls | Wrong namespace, extra latency | Pre-load at proxy level |
| Store data in local files | Shared across all users | Use S3 with namespace prefix |
| Trust user-provided namespace | Prompt injection -> cross-user access | Extract from message envelope |
| Inject raw S3 content into prompt | Code fence escape -> prompt injection | Use `sanitizeWorkspaceContent()` |
| Create new S3Client per request | Memory/connection overhead | Use `getS3Client()` singleton |
| Silently fall back to default-user | All users share one namespace | Fail fast, reject default-user |
| Hardcode file reads in buildUserIdentityContext | Repetitive, error-prone | Add to `WORKSPACE_FILES` array |
