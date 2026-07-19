# Task 7 report: trusted scheduler and read-only occurrences

## Outcome

Task 7 delivers a governed read-only scheduler on branch
`codex/po-v1-task7-scheduler`, built strict RED -> GREEN -> REFACTOR over the
frozen `ScheduleSpecV1` / `ScheduleOccurrenceV1` contracts. It is offline,
credential-free, and public/synthetic only. Every AWS boundary (EventBridge
Scheduler, the DynamoDB control table, and the per-user FIFO) is behind an
injected fake in tests; no test creates a client or makes a network call.

- Base (Gate A): `5e4930e`
- Required subject: `feat(scheduler): add governed read-only schedules`

## Load-bearing invariant: scheduled work cannot dispatch an effect

Proven by two structural layers, both exercised with zero effect calls:

1. IAM (stack). `tests/test_scheduler_stack.py` scans synthesized IAM actions:
   the EventBridge Scheduler role holds ONLY `lambda:InvokeFunction` on the
   ingress function; the ingress role holds ONLY control-table strong-read,
   `sqs:SendMessage` on the update FIFO, and scoped KMS + log writes. Neither
   role holds AgentCore, Secrets Manager, connector, browser, or gateway-invoke
   authority, and no static `AWS::Scheduler::Schedule` is baked in.
2. Runtime path. `scheduler.service.assert_scheduled_turn_operation_allowed`
   derives the allowed operation set from the frozen catalog: only pure reads
   and proposal-only operations (`schedule.list`, workspace/web reads,
   `schedule.propose`, `schedule.cancel.propose`). Any mutation/dispatch class
   op, an unknown op, or a flipped `externalEffects` marker is denied. The
   scheduler enqueues an occurrence body carrying `scheduled=true` /
   `externalEffects=false`; the worker forwards those markers so a scheduled
   `READ_ONLY_AGENT_TURN` can only read or PREPARE a fresh proposal.

## Invariants mapped to tests

- Payload carries only opaque id/generation/fireTime; rejects user content,
  extras, duplicate/non-canonical bytes ->
  `test_models.py::test_schedule_payload_carries_only_opaque_id_generation_firetime`,
  `::test_schedule_payload_rejects_duplicate_and_noncanonical_bytes`.
- Only REMINDER and READ_ONLY_AGENT_TURN via the frozen enum ->
  `test_models.py::test_only_reminder_and_read_only_agent_turn_task_types_accepted`.
- Occurrence id binds schedule+generation+time ->
  `test_models.py::test_occurrence_id_binds_schedule_generation_and_firetime`.
- Opaque schedule id leaks nothing ->
  `test_models.py::test_derive_schedule_id_is_opaque_and_leaks_nothing`.
- Propose -> proposal, no live schedule ->
  `test_service.py::test_propose_creates_proposal_not_a_live_schedule`.
- Confirm ENABLED@rev1 + one opaque EventBridge schedule ->
  `test_service.py::test_confirm_enables_at_revision_one_and_creates_one_eventbridge_schedule_with_opaque_payload`.
- Update bumps generation, replaces schedule, stales old fires ->
  `test_service.py::test_update_bumps_generation_replaces_schedule_and_stales_old_generation_fires`.
- Pause clears next run + deletes live schedule ->
  `test_service.py::test_pause_clears_next_run_and_deletes_live_schedule`.
- Cancel terminal + deletes live schedule ->
  `test_service.py::test_cancel_is_terminal_and_deletes_live_schedule`.
- Fire strong-reads then enqueues exactly one occurrence (group=userId,
  dedupe=occurrenceId) ->
  `test_service.py::test_fire_strong_reads_then_enqueues_exactly_one_occurrence_into_per_user_fifo`.
- Duplicate fire idempotent no-op ->
  `test_service.py::test_duplicate_fire_same_generation_and_time_is_idempotent_noop`.
- Stale generation no-op ->
  `test_service.py::test_stale_generation_fire_is_noop_and_enqueues_nothing`.
- Paused/cancelled/missing fires enqueue nothing ->
  `test_service.py::test_fire_after_pause_or_cancel_enqueues_nothing`,
  `::test_fire_missing_schedule_is_noop`.
- Uncertain EventBridge/SQS persistence => UNCERTAIN, never auto-acts ->
  `test_service.py::test_eventbridge_uncertain_persistence_on_confirm_is_UNCERTAIN_and_never_auto_acts`,
  `::test_sqs_uncertain_persistence_on_fire_is_UNCERTAIN_and_never_auto_acts`.
- Deletion cannot complete while a live schedule exists ->
  `test_service.py::test_deletion_fence_deletes_live_schedules_before_completion`.
- Import creates schedules DISABLED (PAUSED, nextRunAt=None, no schedule) ->
  `test_service.py::test_import_creates_schedules_disabled_with_no_eventbridge_schedule`.
- Ingress rejects poisoned payloads with zero effect calls; region-guarded
  composition injects no effect client ->
  `test_ingress.py::test_ingress_rejects_payload_with_user_content_or_extra_keys_and_makes_zero_effect_calls`,
  `::test_ingress_composition_requires_exact_region_and_injects_no_provider_clients`.
- Scheduled turn cannot dispatch, can only read/propose ->
  `test_no_effects.py::test_scheduled_read_only_turn_cannot_dispatch_connector_or_browser_effect`,
  `::test_scheduled_turn_can_read_or_prepare_a_fresh_proposal_only`.
- Worker at-most-once occurrence via existing ledger; reminder delivers once,
  read-only turn invokes runtime with externalEffects:false ->
  `worker/test_worker.py::test_worker_processes_scheduled_occurrence_at_most_once_reusing_ledger_state_machine`,
  `::test_scheduled_reminder_delivers_once_and_read_only_turn_invokes_runtime_with_external_effects_false`.
- Gateway propose returns a proposal, never a live schedule; confirm/update/
  pause/cancel unreachable through the gateway ->
  `capabilities/test_gateway.py::test_schedule_propose_adapter_returns_proposal_never_a_live_schedule`,
  `::test_confirm_update_pause_cancel_are_not_reachable_through_the_gateway_surface`.
- Stack IAM/least-authority + eu-west-1 + cdk-nag ->
  `tests/test_scheduler_stack.py` (six tests).

## New files

- `lambda/scheduler/__init__.py`
- `lambda/scheduler/models.py`
- `lambda/scheduler/service.py`
- `lambda/scheduler/ingress.py`
- `lambda/scheduler/conftest.py`
- `lambda/scheduler/test_models.py`
- `lambda/scheduler/test_service.py`
- `lambda/scheduler/test_ingress.py`
- `lambda/scheduler/test_no_effects.py`
- `stacks/scheduler_stack.py`
- `tests/test_scheduler_stack.py`

## Modified files

- `app.py` — wire `SchedulerStack` after `RouterStack` (FIFO ARN/URL).
- `lambda/capabilities/gateway.py` — `SchedulePort` + `build_schedule_adapters`
  binding only the three read/propose ops; dispatch stays ADAPTER_DISABLED.
- `lambda/worker/index.py` — fourth `occurrence` branch parsed before
  `QueueEnvelope`, reusing the claim/result/delivery ledger state machine.
- `lambda/web/retention.py` — optional `schedule_store.purge_user_schedules`
  in `_purge_once`; deletion stays pending until zero ENABLED schedules remain.

## Deviations / flags

- `message_queue.py` is NOT in Task 7's file list, so the occurrence rides the
  per-user FIFO as a distinguishable SQS body (`ScheduledOccurrence`,
  schema `personal-operator.schedule-occurrence-body.v1`) that the worker
  detects and parses before `QueueEnvelope.from_json`. FLAG for reviewers: if a
  first-class envelope kind is preferred, `message_queue.py` must join the file
  list.
- The scheduler service's concrete DynamoDB/EventBridge/SQS adapters are wired
  by deployment composition via `ingress.configure_service_factory`; the
  trusted ingress module intentionally keeps those clients off its import
  surface and fails closed on region drift.
- Offline `cdk synth` for `PersonalOperatorScheduler` succeeds. Full-app synth
  additionally requires a built `web/dist/index.html` and VPC AZ context/
  credentials; both are pre-existing environment preconditions unrelated to
  Task 7.
