# Task 8 — Networkless Linux compute capsule

Single-writer worktree `/private/tmp/personal-operator-v1-task8-compute`,
branch `codex/po-v1-task8-compute`, from Gate A `5e4930e`. Strict
RED -> GREEN -> REFACTOR. Offline, credential-free, public/synthetic only. No
real Docker container, network, or AWS call was made; the job runner and
sandbox are modeled behind local test fakes.

## Open external gates (NOT run locally, NOT claimed)

- Real Docker build of `compute/Dockerfile` is OPEN.
- ARM64 image build/publish and the immutable digest resolution are OPEN.
- Static image/security scan of the runner image is OPEN.

`compute/Dockerfile` and `compute/seccomp.json` are SHAPE ONLY. `ComputeStack`
and `app.py` accept a precomputed pinned image digest as explicit CDK context
so synth stays offline; the digest the reviewed pipeline resolves replaces the
placeholder before any build.

## What was built

- `lambda/compute/models.py` — frozen `SMALL` ResourceProfile (deadline, CPU,
  memory/OOM, pids/fork-bomb, file-size, output count/single/total caps);
  content-addressed `derive_job_id` (dedupe key over grant.sub, invocationId,
  argsHash); `derive_input_digest` over the sorted content-hashed manifest;
  `build_job_spec` bound to the single pinned image digest.
- `lambda/compute/importer.py` — fail-closed output validation
  (symlink/hardlink/device/fifo/socket/non-regular rejection, unsafe path via
  `_safe_path`, single-file/total/count caps, read-then-restat mutation
  detection, resolve-inside-root) then all-or-nothing import under
  `<userId>/jobs/<jobId>/` and a content-addressed `ComputeReceiptV1`.
- `lambda/compute/service.py` — `ComputeService` (stage immutable inputs into a
  fresh per-job root, build spec, invoke injected runner, finalize with
  atomic import or non-success receipt, tear down the job root) plus the two
  submit-only capability adapters. Idempotent short-circuit on an existing
  receipt. Status reads are strictly scoped to `grant.sub` and never leak a
  foreign receipt.
- `compute/runner.py` (+ package copy `lambda/compute/runner.py`) — networkless
  namespace fence (DNS, connect, create_connection), ambient-authority drop,
  POSIX rlimits (CPU/AS/NPROC/FSIZE), new process group + `killpg` tree kill,
  credential-provider fail-closed. Root shim delegates to the package module.
- `compute/Dockerfile`, `compute/seccomp.json` — shape-only image contract.
- `stacks/compute_stack.py` — isolated VPC (no NAT, no VPC endpoints, no public
  IPs, no internet route), ARM64 Fargate task, read-only root fs, non-root
  user, task role that can only `s3:GetObject` inputs and `s3:PutObject` under
  the per-job output prefix, KMS bound by S3 via-service. No ambient providers.
- `app.py` — instantiates `ComputeStack`, pins the image digest; `composition.py`
  gains a compute-only injection seam (the credential-free Lambda holds no
  compute authority unless the two compute adapters are explicitly injected).

## Hostile cases covered (RED-first)

immutable image digest; input hashes / input digest binding; per-turn quota
(3rd compute.run denied by pack maxCallsPerTurn=2) and oversized/missing input;
invalid input paths (absolute, backslash, traversal, control char); output
symlink; output hardlink; device/fifo/socket nodes; single-file / total-bytes /
file-count overflow; output changed between read and re-stat; timeout ->
TIMED_OUT + tree kill + no outputs; OOM -> FAILED + tree kill; fork bomb ->
FAILED + tree kill; three-user Cartesian cross-user isolation (no foreign
receipt leak); DNS blocked; internet TCP refused; VPC-endpoint refused; IMDS
169.254.169.254 unreachable; boto3 credential chain resolves to nothing; atomic
import + content receipt with partial-failure rollback; gateway wiring returns
`{jobId,status:"QUEUED"}` and rejects oversized adapter output; gateway/runtime
holds no direct compute or credential authority (ADAPTER_DISABLED without
injection); synth isolation + IAM parity.

## Verification

- `python -m pytest -q lambda/compute` -> 43 passed.
- `python -m pytest -q lambda/compute lambda/capabilities tests/test_compute_stack.py tests/test_capability_stack.py`
  -> all passed (no regression in touched areas).
- `git diff --check 5e4930e..HEAD` -> clean.

## Deviations

- The AF_UNIX socket-node test binds from a short cwd to dodge the macOS path
  cap; behavior is identical to a socket node in the output tree.
- The output store's atomicity is modeled by an all-or-nothing `commit_job`
  contract (temp+rename semantics) in the injected store, per the plan.
