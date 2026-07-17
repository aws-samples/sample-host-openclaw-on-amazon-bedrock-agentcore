# Upstream Source Ledger

## AWS sample foundation

- Repository: `https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore`
- Imported commit: `e13e385ec44a3776e571ec48001904e9394cc20e`
- Local remote name: `upstream`
- License: MIT No Attribution (MIT-0), retained unchanged in `LICENSE`
- Import status: experimental reference implementation; it is not accepted as
  a production security or authorization boundary

The `upstream` fetch and push URLs remain pointed at the AWS sample. Local
product work is based on the exact imported commit above and is reviewed as a
patch against it.

## OpenClaw runtime source

- Repository: `https://github.com/openclaw/openclaw.git`
- Audited source commit: `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438`
- Package version declared by that commit: `2026.7.2`
- Package license declared by that commit: MIT
- Supported Node range declared by that commit:
  `>=22.22.3 <23 || >=24.15.0 <25 || >=25.9.0`
- Package manager declared by that commit: `pnpm@11.2.2` with its integrity
  hash in `package.json`

OpenClaw `2026.7.2` was not present in the npm registry when checked on
2026-07-17: `npm view openclaw@2026.7.2 version --json` returned `E404` with
`No match found for version 2026.7.2`. The available beta is not substituted
because it is not the approved runtime.

`bridge/Dockerfile` therefore fetches the immutable source commit, verifies
both `git rev-parse HEAD` and the declared package version, enables the source's
integrity-pinned package manager, installs from its frozen lockfile, and builds
the runtime. It does not perform a mutable npm install of OpenClaw or install a
community marketplace CLI or skill.

## Runtime release policy

- Builder and runtime use Node.js `24.15.0-slim`; local verification uses an
  installed Node.js 24 release at or above that floor.
- OpenClaw remains pinned by full Git commit, not by a moving branch or npm
  tag.
- A release image must additionally be recorded by immutable ECR digest and
  accompanied by an SBOM. Task 1 does not build, push, or release an image.
- Community skills, arbitrary package installation, and provider credentials
  are not inherited from the sample image.

## Reuse policy

Upstream changes are imported intentionally by commit and reviewed as source.
Product credentials and effect authority remain outside OpenClaw. No upstream
feature is considered accepted merely because it existed in the imported
sample.
