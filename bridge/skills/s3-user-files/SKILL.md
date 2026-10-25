---
name: s3-user-files
description: Per-user persistent file storage backed by AWS S3. Read, write, list, and delete text files isolated per user. Each user has a private S3 namespace — no cross-user data leakage. Use when the user asks to save, remember, or persist any information across sessions.
allowed-tools: Bash(node:*)
---

# S3 User Files

Per-user persistent file storage backed by AWS S3. Each user's files are stored in an isolated namespace — no cross-user data leakage.

## Important

**Always use the user_id from the system prompt** when calling these tools.
Never hardcode or guess a user_id. The system provides it automatically.

## Usage

### read_user_file

Read a file from the user's persistent storage.

```bash
node {baseDir}/read.js <user_id> <filename>
```

- `user_id` (required): The user's unique identifier (e.g., `telegram_12345`)
- `filename` (required): The file name to read (e.g., `notes.md`)

### write_user_file

Write content to a file in the user's persistent storage.

```bash
node {baseDir}/write.js <user_id> <filename> <content>
```

- `user_id` (required): The user's unique identifier
- `filename` (required): The file name to write
- `content` (required): The text content to write

### list_user_files

List all files in the user's persistent storage.

```bash
node {baseDir}/list.js <user_id>
```

- `user_id` (required): The user's unique identifier

### delete_user_file

Delete a file from the user's persistent storage.

```bash
node {baseDir}/delete.js <user_id> <filename>
```

- `user_id` (required): The user's unique identifier
- `filename` (required): The file name to delete

### presign_user_file

Generate a temporary download URL (presigned URL) for a file. The URL is valid for 1 hour by default.

```bash
node {baseDir}/presign.js <user_id> <filename> [expires_in_seconds]
```

- `user_id` (required): The user's unique identifier
- `filename` (required): The file name to generate a URL for
- `expires_in_seconds` (optional): URL validity in seconds (default: 3600, max: 604800)

**Important**: Always use this tool to generate presigned URLs. Do NOT use `aws s3 presign` CLI — it may produce SigV2 signatures that fail on KMS-encrypted buckets.

### download_user_file

Copy a file (including binary files such as ZIP archives) from the user's storage into the local sandbox as raw bytes. Prints the local path. Does not extract archives.

```bash
node {baseDir}/download.js <user_id> <filename> [output_dir]
```

- `user_id` (required): The user's unique identifier
- `filename` (required): The file name, e.g. `_uploads/doc_1700000000_ab12cd34.zip`
- `output_dir` (optional): Absolute directory to write into (default: `/tmp/openclaw-uploads/<user_id>/`)
- Files larger than 25 MB are refused.

## Files the user sends in chat

When a user sends a Markdown (`.md`) file or a ZIP (`.zip`) archive, the router stores it under `_uploads/` in the user's storage and the message tells you the exact filename (e.g. `_uploads/doc_1700000000_ab12cd34.md`). Use that filename as-is with read_user_file, download_user_file or presign_user_file.

- Markdown: small files are already inlined in the message; use read_user_file for the full text of a truncated one.
- ZIP: the message includes a listing from the archive's central directory and flags unsafe entries. To work with the contents, download_user_file the archive and extract it in the sandbox (for example with Python's `zipfile`), skipping entries flagged UNSAFE PATH or SYMLINK and checking sizes as you extract.

## From Agent Chat

- "Save my preferences" -> write_user_file with the user's preferences
- "What do you remember about me?" -> read_user_file to check stored notes
- "What's your name?" -> use the Agent Identity section already in the system prompt
- "Forget everything about me" -> delete_user_file on each stored file
- "What files do you have for me?" -> list_user_files
- "Give me a download link for that file" -> presign_user_file
- "Generate a presigned URL for my screenshot" -> presign_user_file

## Security Notes

- Files are isolated per user via S3 key prefix: `{user_id}/{filename}`
- user_id uses underscores (e.g., `telegram_12345`) — colons are replaced
- Content encrypted at rest via S3 server-side encryption
- Bucket enforces SSL-only access
- Path traversal attempts are sanitized (removes `..`, restricts characters)
- Never use `default_user` as user_id — scripts reject it with an error

## Reserved filenames (NOT writable through this skill)

The filenames `AGENTS.md`, `SOUL.md`, `USER.md`, `IDENTITY.md`, `TOOLS.md`,
and `MEMORY.md` are reserved for workspace files that the Bedrock proxy
concatenates into the model's system prompt. They are sourced from the
operator prefix `_system/{user_id}/` at the bucket root and are not writable through
`write_user_file`, `delete_user_file`, or the gateway MCP `write_file` /
`delete_file` tools; attempts return an error. Any `write_user_file` call
that targets one of these filenames (or the `_system/` sub-prefix) is
refused by `buildKey`. Store user notes under a different filename
(e.g. `notes.md`, `preferences.md`).
