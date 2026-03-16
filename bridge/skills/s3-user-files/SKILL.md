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
- `filename` (required): The file name to read (e.g., `IDENTITY.md`)

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

## From Agent Chat

- "Save my preferences" -> write_user_file with the user's preferences
- "What do you remember about me?" -> read_user_file to check stored notes
- "What's your name?" -> read_user_file IDENTITY.md for this user
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
