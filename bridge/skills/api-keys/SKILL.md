---
name: api-keys
description: Dual-mode API key storage — native file-based or AWS Secrets Manager. Store, retrieve, list, and delete API keys securely. Use manage_native for simple file storage, manage_secret for Secrets Manager with audit trail. Use retrieve to look up a key from either backend. Use migrate to move keys between backends.
allowed-tools: Bash(node:*)
---

# API Key Management

Dual-mode API key storage with native file-based and AWS Secrets Manager backends.

## Important

**Always use the user_id from the system prompt** when calling these tools.
Never hardcode or guess a user_id. The system provides it automatically.

## Native File Storage (manage_native)

Store API keys in a local JSON file (`.openclaw/user-api-keys.json`), synced to S3 with KMS encryption at rest.

```bash
node {baseDir}/native.js <user_id> <action> [key_name] [key_value]
```

- `user_id` (required): The user's namespace (e.g., `telegram_12345`)
- `action` (required): `set`, `get`, `list`, or `delete`
- `key_name` (required for set/get/delete): Alphanumeric key name
- `key_value` (required for set): The API key value to store

### Examples

```bash
node {baseDir}/native.js telegram_12345 set my_api_key sk-abc123
node {baseDir}/native.js telegram_12345 get my_api_key
node {baseDir}/native.js telegram_12345 list
node {baseDir}/native.js telegram_12345 delete my_api_key
```

## Secrets Manager Storage (manage_secret)

Store API keys in AWS Secrets Manager with KMS encryption, audit trail via CloudTrail, and per-user isolation.

```bash
node {baseDir}/secret.js <user_id> <action> [key_name] [key_value]
```

- `user_id` (required): The user's namespace (e.g., `telegram_12345`)
- `action` (required): `set`, `get`, `list`, or `delete`
- `key_name` (required for set/get/delete): Alphanumeric key name
- `key_value` (required for set): The secret value to store

### Examples

```bash
node {baseDir}/secret.js telegram_12345 set my_api_key sk-abc123
node {baseDir}/secret.js telegram_12345 get my_api_key
node {baseDir}/secret.js telegram_12345 list
node {baseDir}/secret.js telegram_12345 delete my_api_key
```

## Unified Retrieval (retrieve)

Look up a key from either backend — checks Secrets Manager first, falls back to native file.

```bash
node {baseDir}/retrieve.js <user_id> <key_name>
```

## Migrate Between Backends

Move a key from native to Secrets Manager or vice versa.

```bash
node {baseDir}/migrate.js <user_id> <key_name> <direction>
```

- `direction`: `native-to-secure` or `secure-to-native`

## From Agent Chat

- "Store my OpenAI key securely" -> manage_secret set
- "Save this API key" -> manage_native set (simpler)
- "What API keys do I have?" -> manage_native list + manage_secret list
- "Get my OpenAI key" -> retrieve (checks both backends)
- "Move my key to Secrets Manager" -> migrate native-to-secure
- "Delete my API key" -> manage_native delete or manage_secret delete

## Security Notes

- Native mode: Keys stored in `.openclaw/user-api-keys.json`, synced to S3 with KMS encryption
- Secrets Manager mode: Keys stored at `openclaw/user/{namespace}/{key_name}`, auditable via CloudTrail
- Per-user isolation via STS session-scoped credentials
- Max 10 secrets per user in Secrets Manager
- Key names: alphanumeric, starting with letter, max 64 chars
