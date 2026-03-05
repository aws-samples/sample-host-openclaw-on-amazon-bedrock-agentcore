# Security

## Security Architecture

This solution applies **defense-in-depth** across network, application, identity, and data layers:

- **Network isolation**: Private VPC subnets with 7 VPC endpoints; no direct internet exposure for containers
- **Webhook authentication**: Cryptographic validation (Telegram secret token, Slack HMAC-SHA256 with replay protection)
- **Per-user microVM isolation**: Each user runs in a dedicated Firecracker microVM on AgentCore Runtime
- **STS session-scoped credentials**: Container assumes its own role with a session policy restricting S3, DynamoDB, Secrets Manager, and EventBridge to the user's namespace only
- **Secure API key management**: Built-in `api-keys` skill stores user secrets in AWS Secrets Manager (KMS-encrypted, CloudTrail-auditable) — replaces insecure plaintext `.env` files
- **Encryption**: All data encrypted at rest (KMS CMK) and in transit (TLS)
- **Least-privilege IAM**: Tightly scoped permissions per component
- **Tool hardening**: OpenClaw `read` tool denied to prevent credential access; `exec` allowed with STS-scoped blast radius
- **Automated compliance**: cdk-nag AwsSolutions checks on every `cdk synth`

For the complete security architecture — threat model, all 10 defense-in-depth layers, compliance details, operations runbook, and extension roadmap — see **[docs/security.md](docs/security.md)**.

## Reporting Security Issues

See [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for information on reporting security vulnerabilities.
