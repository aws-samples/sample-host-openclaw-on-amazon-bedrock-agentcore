# Task 3 report — generic connector action kernel

## Outcome

Extracted a connector-generic action kernel from the Gmail-specific action code
using the ADAPTER WRAPPER approach (no refactor-in-place). Every existing Gmail
public signature and stored-record shape is unchanged; every fence/hash/expiry
guard remains inside the untouched `GmailSendExecutor`/`GmailEffectReconciler`.

Baseline: HEAD `5e4930e`, `lambda/actions` = 100 passed.

## Commands run (interpreter: fallback `/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python`; worktree `.venv` does not exist)

- Focused: `<py> -m pytest -q lambda/actions` -> **119 passed** (100 preexisting + 18 new connector contract + 1 new proposal-compat assertion in test_state_machine; +1 augmented gmail-send receipt assertion is in-place).
- Broader: `<py> -m pytest -q lambda/actions lambda/web lambda/workflows/gmail tests/integration` -> **426 passed**.
- Full aggregate: `PYTHON=<py> PATH="/opt/homebrew/opt/node@24/bin:$PATH" ./scripts/test-local.sh` -> final line **`All local checks passed.`** (grep count = 1; not trusting shell exit code).
- `git diff --check` -> clean (no whitespace errors).

## Property-by-property mapping (each enforced by CODE, proven by a contract test)

1. No dispatch without an exact persisted APPROVED proposal —
   `test_kernel_refuses_dispatch_from_unsaved_proposal`. Guard: `GmailSendExecutor.execute`
   re-loads via `machine.get` and raises `SendValidationError("action does not exist")`
   (gmail_send.py:461-463); `GmailConnectorAdapter.dispatch` delegates 1:1.
2. No credential resolution before admission —
   `test_credentials_resolved_only_after_adapter_admitted`. A counting lazy-creds provider
   proves resolver.calls==0 for (a) non-founder (`CapabilityDenied`, founder gate
   gmail_send.py:490-491) and (b) deletion-fenced (`EffectUncertain` before provider,
   gmail_send.py:595-599), and ==1 only on a founder+fence-live APPROVED dispatch.
3. Exact binding rechecked at dispatch —
   `test_dispatch_rechecks_full_binding_and_never_sends_on_mismatch` (10 params:
   founder/actionId/capability/resource/connectionId/approvedArgsHash/approvalArgsHash/
   approvedDraftRevision/approvalDraftRevision/approvalExpiresAt<=approvedAt). Guard:
   equality gate gmail_send.py:493-509. Asserts `provider.send_calls==[]` and no transitions.
4. Deletion fence rechecked immediately before dispatch —
   `test_deletion_fence_rechecked_after_claim_before_provider`. Toggling fence (False at
   admission, True once DISPATCHING claim is held) -> `EffectUncertain`, state UNCERTAIN,
   `uncertaintyReason=="account-deletion-fence"`, `send_calls==[]` (gmail_send.py:594-599,611-636).
5. Ambiguous provider stays UNCERTAIN, never replays —
   `test_ambiguous_provider_evidence_quarantines_uncertain_without_replay`. First dispatch ->
   UNCERTAIN (gmail_send.py:611-636); second dispatch on the same record ->
   `EffectUncertain("...requires provider reconciliation")` (gmail_send.py:537-538); send_calls
   stays length 1.
6. No resend before reconciliation —
   `test_dispatching_or_uncertain_reconciles_never_redispatches` (DISPATCHING, UNCERTAIN).
   `dispatch` raises `EffectUncertain` with `send_calls==[]`; `reconcile` confirms on full SENT
   evidence (find_calls==1, state CONFIRMED) and returns None on mismatched payload leaving
   state unchanged (reconcile.py:79-160).
7. Local draft edit atomically stales pending approval —
   `test_new_draft_revision_atomically_stales_pending_founder_approval_via_kernel`.
   `GmailConnectorAdapter.supersede_pending` routes `ApprovalService.mark_stale`
   (state_machine.py:571-610); resulting state STALE with `supersededByDraftRevision==5`;
   the old revision can no longer be approved (`ConcurrentActionUpdate`). Atomic fence:
   repository.py:272-277,555-558.

Plus `test_existing_types_satisfy_generic_protocols` (connectors) and
`test_draft_revision_satisfies_action_proposal_v1_without_stored_shape_change`
(state_machine) prove the runtime-checkable Protocol registrations.

## New files

- `lambda/actions/proposals.py` — `ActionProposalV1` (runtime_checkable Protocol) +
  `GenericActionProposalV1` frozen dataclass. Dual-import guard (loadable as `action_proposals`).
- `lambda/actions/receipts.py` — `EffectReceiptV1` (runtime_checkable Protocol), non-invasive;
  loadable as `action_receipts`.
- `lambda/actions/connectors.py` — `ConnectorContext`/`ConnectorAdapter` Protocols +
  `GenericConnectorKernel` (pure delegation) + `GmailConnectorAdapter` wrapper. Dual-import guard
  (loadable as `action_connectors`).
- `lambda/actions/test_connectors.py` — the 7 contract tests + protocol registration test.

## Additive-only modifications

- `models.py`: `DraftRevision.capability`/`.connection_ref` and
  `EffectReceipt.capability`/`.resource`/`.connection_ref`/`.provider_effect_id`/`.evidence_labels`
  read-only properties. No `__post_init__`, no `record()`/`from_record()` key-set change.
- `gmail_send.py`, `reconcile.py`: `# implements ...` registration comments only; no logic edits.
- `__init__.py`: export the new generic symbols alongside existing exports.
- `test_gmail_send.py`: augment the happy-path test with `EffectReceiptV1` compat + record()-keyset
  invariance assertions.
- `test_state_machine.py`: add the `ActionProposalV1` structural-compat test.

## Deviations from the plan (with justification)

- **Property-7 scope choice (ii), not (i).** The plan flagged a decision: expose the kernel
  supersede method + contract test now (ii) vs also wire the `web/gmail_workspace.py edit_draft`
  call site (i, which expands the file list beyond Task 3's listed set, plan lines 167-178). I chose
  (ii): `GmailConnectorAdapter.supersede_pending` routes `ApprovalService.mark_stale` and the contract
  test proves the atomic staling + that the old revision can no longer be approved. Wiring the
  `edit_draft` call site is left as an explicit Gate-A follow-up because `gmail_workspace.py` is
  outside Task 3's file set and touching it risks the workspace draft-edit path (two different
  `DraftRevision` classes). Flagging to the orchestrator.
- **`revoke` is an injected-or-no-op delegate.** For the Gmail send adapter, `revoke` delegates to an
  injected `connection_revoker.revoke_all(connection_ref)` or is a documented effect-path no-op; the
  adapter never holds provider creds (revocation lives in `web/composition.py`). No new revoker wired
  into composition (out of Task 3 file set).
- **`read` operations are named** (`verify_bound_account`, `find_by_message_id`) mapping to the two
  existing read-only provider calls; no new provider surface added.
