# Task 9 report: portable state v2 and staged import

## Outcome

Task 9 (portable content-addressed state transfer) is implemented on branch
`codex/po-v1-task9-portable`, cut from Gate A `5e4930e`. Strict
RED -> GREEN -> REFACTOR. The subject is networkless, credential-free, and uses
public/synthetic data only. It was not deployed, pushed, or connected to any
provider, AWS account, or cloud runtime; every store is a fake in tests.

- Base (Gate A): `5e4930e`
- Final commit / tree: see the "Final artifacts" section (filled at commit).
- Required subject: `feat(portable): add content addressed state transfer`

## Delivered contracts

### Deterministic content-addressed export (`portable/exporter.py`)

- `PortableExporter.build(user_id)` reuses the v1 source contract
  (`records_for_user` + `workspace_files`) so composition's `_ExportSource` is
  unchanged.
- Byte-reproducible ZIP: fixed 1980 timestamps, `0o600` attrs, sorted entries,
  deflate level 6. Two independent builds produce identical bytes AND identical
  complete-bundle hash.
- Per-object descriptor `{path, category, type, size, sha256}` for every entry;
  manifest namelist equals the zip namelist (minus the `manifest.json` frame).
- Format tag `personal-operator.portable.v2` (no v1 compat alias).
- Include allowlist `{memory, schedules, receipts, workspace}`; any other record
  category is rejected. Credentials/sessions/grants/approvals/runtime internals
  are never serialized (they never reach the source, and the allowlist is
  enforced again here).
- Landing state stamped into the manifest: schedules `DISABLED`, connectors
  `DISCONNECTED`, receipts `{replayable: false}`.

### Complete-bundle hash (`portable/manifest.py`)

- Single canonical `safe_path` validator now lives here; `web/retention.py`
  imports it and wraps its rejection in the historical `ExportBoundaryError`
  (no v1 behavior change; 29 retention tests still green).
- `complete_bundle_hash` = SHA-256 over canonical JSON of the manifest, which
  embeds every object's SHA-256 plus format tag and exporting `userId`. This
  decouples activation binding from ZIP framing while transitively covering all
  content.

### Staged import (`portable/importer.py`)

- `build_plan(bundle)` is a pure DRY-RUN: total, hostile-input-aware parsing
  (content addressing, canonical-JSON framing, path safety, category/prefix
  agreement, no extra/missing/duplicate entries), then policy checks, then a
  typed `ImportPlanV1` with per-category counts, landing, owner claim, and the
  bundle hash. No store writes; proven by a store double whose `swap` asserts.
- Rejections: hash mismatch, size mismatch, non-canonical manifest, path
  traversal, malformed/non-zip, missing manifest, v1 tag, secret-shaped keys,
  active authority (`CONNECTED`/envelope/`refreshToken`), pending/uncertain
  effects, deletion tombstones, and any receipt landing not stamped
  non-replayable.
- `activate(bundle, approved_bundle_hash, target_user_id)` recomputes the
  complete-bundle hash and requires EXACT equality to the caller-approved hash;
  binds to the CALLER identity, never an embedded owner claim (three-user
  isolation). Landing normalizes schedules to `DISABLED` and strips any armed
  `nextRun`/`nextRunAt`; connectors never materialize an envelope; receipts land
  non-replayable.

### Atomic activation CAS (`portable/staging.py`)

- `DynamoStagedImportStore` performs a single conditional generation
  compare-and-swap (imitating the retention sweep cursor CAS). Stale generation
  -> `ImportRejected` with zero partial state; store outage -> `ImportUncertain`
  (never a silent success). All-or-nothing.

### HTTP boundary (`web/index.py`) and wiring (`web/composition.py`)

- `WebApplication(importer=...)` port added. Routes `POST /api/import/plan`
  (CSRF; dry-run plan + bundleHash; no mutation) and `POST /api/import/activate`
  (CSRF; exact-hash bound; 200 on CAS success, 400 on `ImportRejected` /
  malformed / unconfirmed, 409 on `ImportUncertain`). Both added to
  `known_paths`. Production wires `PortableImporter(DynamoStagedImportStore(
  control_table))`.

### Frontend (`web/src/App.jsx`, `styles.css`)

- `ImportPage`: file -> base64 -> dry-run plan preview with per-category counts
  and the exact bundle hash -> checkbox confirm of that hash gates the activate
  button -> `POST /api/import/activate`. Route `/import`, nav link, and
  `STATIC_RETURN_PATHS` entry added.

## Invariants mapped to tests

- byte reproducibility -> `test_exporter.py::test_byte_reproducibility_across_independent_calls`
- per-object coverage + format tag -> `test_exporter.py::test_per_object_coverage_and_format_tag`
- landing stamps -> `test_exporter.py::test_landing_states_are_stamped`
- include/exclude categories -> `test_exporter.py::test_include_exclude_categories_reject_unknown_record_category`
- credentials never in bytes -> `test_exporter.py::test_credentials_never_appear_in_bundle_bytes`
- path traversal (export) -> `test_exporter.py::test_unsafe_workspace_paths_rejected`
- deletion exclusion regression -> `test_exporter.py::test_deletion_exclusion_regression_matches_adapter_categories`
- dry-run no mutation -> `test_importer.py::test_dry_run_plan_has_counts_and_hash_and_never_writes`
- hash / size / noncanonical / duplicate / traversal / malformed+v1 ->
  `test_importer.py::test_hash_mismatch_rejected`, `test_size_mismatch_rejected`,
  `test_noncanonical_manifest_rejected`,
  `test_duplicate_or_extra_or_missing_paths_rejected`,
  `test_path_traversal_rejected`, `test_malformed_bundle_and_v1_tag_rejected`
- secret corpus -> `test_importer.py::test_secret_corpus_rejected`
- active authority / pending effects / tombstone / replay ->
  `test_importer.py::test_active_authority_rejected`,
  `test_pending_effects_rejected`, `test_deletion_tombstone_rejected`,
  `test_replay_rejected_when_landing_not_stamped_nonreplayable`
- activation exact-hash binding -> `test_importer.py::test_activation_bound_to_exact_bundle_hash`
- CAS atomicity + failure atomicity -> `test_importer.py::test_activation_cas_atomic_on_stale_generation`,
  `test_failure_atomicity_leaves_no_partial_state`
- schedules disabled / connectors disconnected / receipts non-replayable ->
  `test_importer.py::test_activation_success_lands_disabled_disconnected_nonreplayable`
- three-user isolation -> `test_importer.py::test_three_user_isolation_binds_to_caller_identity`
- staging CAS store -> `test_staging.py` (advance from zero, stale rejection,
  outage uncertainty, oversize rejection)
- routes -> `web/test_index.py::test_import_plan_requires_csrf_and_returns_dry_run`,
  `::test_import_activate_binds_hash_and_maps_status_codes`
- v1 export regression -> `web/test_retention.py` (29 passed)
- web import page -> `web/src/App.test.jsx` ("previews a dry-run import plan and
  gates activation on the exact bundle hash")

## New files

- `lambda/portable/__init__.py`
- `lambda/portable/manifest.py`
- `lambda/portable/exporter.py`
- `lambda/portable/importer.py`
- `lambda/portable/staging.py`
- `lambda/portable/test_exporter.py`
- `lambda/portable/test_importer.py`
- `lambda/portable/test_staging.py`

## Modified files

- `lambda/web/retention.py` (lift `_safe_path` to the shared validator)
- `lambda/web/index.py` (importer port + two routes + funnel)
- `lambda/web/composition.py` (wire `PortableImporter` + `DynamoStagedImportStore`)
- `lambda/web/test_index.py` (importer fake + route tests)
- `web/src/App.jsx`, `web/src/App.test.jsx`, `web/src/styles.css` (ImportPage)

## Deviations from plan

- Added `ImportUncertain` (a `PortableError` subclass) distinct from
  `ImportRejected` so the boundary can map uncertain CAS outcomes to 409 and
  deterministic rejections to 400, matching the plan's status-code contract.
  This was not in the original error list but is required to keep the funnel
  truthful (uncertain != rejected).
- Added `portable/staging.py` with `DynamoStagedImportStore` as the production
  CAS store (the plan described the CAS pattern but did not name a module);
  keeping it in the portable package preserves the single-owner boundary.
- Import routes accept the bundle as base64 in the JSON body (`bundle`) rather
  than a separate binary channel, matching the existing base64 export contract.

## Test evidence

- `python -m pytest -q lambda/portable lambda/web` -> 255 passed.
- `python -m pytest -q lambda/portable lambda/web lambda/actions` -> 355 passed.
- `npm test` (web) -> 13 passed.
- `git diff --check 5e4930e..HEAD` -> clean.
