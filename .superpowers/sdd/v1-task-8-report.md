# Task 8 — inactive compute reference; operational completion OPEN

Single-writer worktree `/private/tmp/personal-operator-v1-task8-compute`,
branch `codex/po-v1-task8-compute`, from Gate A `5e4930e`. Strict
RED -> GREEN -> REFACTOR. Offline, credential-free, public/synthetic only. No
real Docker container, network, or AWS call was made; the job runner and
sandbox are modeled behind local test fakes.

## Final-audit safe-release correction

The integrated audit found that source-local contracts and a synthesized stack
did not provide a concrete credential-free staging, launch, and collection
transport. The active application therefore no longer imports or instantiates
`ComputeStack` and no longer requires `compute_image_digest`. Production
composition injects no compute adapters, so both catalog operations return
`ADAPTER_DISABLED`. The standalone stack, Dockerfile, transport contracts, and
runner remain inactive reference material. Same-interpreter Python API fences
are defense in depth, not a security or isolation boundary. Image, launcher,
live isolation, and Task 8 operational completion remains OPEN.

## Open external gates (NOT run locally, NOT claimed)

- Real Docker build of `compute/Dockerfile` is OPEN.
- ARM64 image build/publish and the immutable digest resolution are OPEN.
- Static image/security scan of the runner image is OPEN.
- Credential-free staging, launch, and output collection are OPEN.
- Exact-task launch plus ENI/flow-log isolation evidence is OPEN.

`compute/Dockerfile`, `compute/seccomp.json`, and `ComputeStack` are INACTIVE
NON-PRODUCTION SHAPE ONLY. They are not instantiated or required by `app.py`.

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
- `compute/runner.py` (+ package copy `lambda/compute/runner.py`) — local
  same-interpreter API fences (DNS, socket, process creation), environment scrub,
  POSIX rlimits (CPU/AS/NPROC/FSIZE), new process group + `killpg` tree kill,
  credential-provider fail-closed. The inactive image resolves the package
  entrypoint directly; the repository root shim is local reference only.
- `compute/Dockerfile`, `compute/seccomp.json` — shape-only image contract.
- `stacks/compute_stack.py` — inactive isolated-VPC reference (no NAT, no VPC
  endpoints, public IPs, or internet route), ARM64 Fargate task, read-only root
  fs, non-root user, and no workload task role. Its ECS execution role is for
  the exact ECR repository and runner log group only.
- `app.py` — intentionally instantiates no compute stack and requires no compute
  image digest; `composition.py` retains a test-only injection seam while the
  active default has no compute adapters and returns `ADAPTER_DISABLED`.

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
