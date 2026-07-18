"""Contract tests for the connector-generic action kernel (Task 3).

Each of the 7 required properties gets a failing-first contract test that
proves the guard is enforced by CODE through the connector surface. The tests
reuse the existing Gmail fixtures (loaded the same importlib way the concrete
suites use) so the wrapper is exercised against the real executor/reconciler
and their untouched guards.
"""

import importlib.util
from pathlib import Path
import sys

import pytest


ACTIONS_DIR = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ACTIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Reuse the concrete fixtures verbatim. These modules load the concrete kernel
# modules internally (as action_models/action_gmail_send/...); we bind to THOSE
# same module objects so exception/class identity matches what the executor and
# reconciler actually raise (avoiding the dual-load identity trap).
gmail_fixtures = load("test_action_gmail_send_fixtures", "test_gmail_send.py")
state_fixtures = load("test_action_state_machine_fixtures", "test_state_machine.py")

models = gmail_fixtures.models
machine_module = gmail_fixtures.machine_module
send_module = gmail_fixtures.send_module
reconcile_module = gmail_fixtures.reconcile_module
proposals_module = load("action_proposals", "proposals.py")
receipts_module = load("action_receipts", "receipts.py")
connectors_module = load("action_connectors", "connectors.py")

ActionState = models.ActionState
GmailConnectorAdapter = connectors_module.GmailConnectorAdapter
ConnectorAdapter = connectors_module.ConnectorAdapter
ActionProposalV1 = proposals_module.ActionProposalV1
EffectReceiptV1 = receipts_module.EffectReceiptV1

action = gmail_fixtures.action
Provider = gmail_fixtures.Provider
Repository = gmail_fixtures.Repository
provider_evidence = gmail_fixtures.provider_evidence
operation_ids = gmail_fixtures.operation_ids
message_id = gmail_fixtures.message_id
NOW = gmail_fixtures.NOW


def build_adapter(
    record,
    *,
    provider=None,
    founders={"founder-1"},
    repo=None,
    deletion_blocked=lambda _user_id: False,
):
    executor, repo, provider = gmail_fixtures.executor(
        record,
        provider=provider,
        founders=founders,
        repo=repo,
        deletion_blocked=deletion_blocked,
    )
    adapter = GmailConnectorAdapter(executor=executor, provider=provider, repository=repo)
    return adapter, repo, provider


# ---------------------------------------------------------------------------
# Structural registration: the existing types satisfy the generic contracts.
# ---------------------------------------------------------------------------
def test_existing_types_satisfy_generic_protocols():
    assert isinstance(GmailConnectorAdapter(executor=object()), ConnectorAdapter)
    draft = models.DraftRevision(
        action_id="action_12345678",
        user_id="founder-1",
        draft_revision=4,
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        sender_address="founder@example.com",
        args={"to": "person@example.net", "subject": "Hi", "body": "Body"},
        created_at=NOW,
    )
    assert isinstance(draft, ActionProposalV1)
    assert draft.capability == "gmail.send"
    assert draft.connection_ref == "google_conn_1234"
    receipt = models.EffectReceipt.from_provider_evidence(
        provider_evidence(
            message_id="<po-" + "0" * 24 + "@personal-operator.invalid>",
            payload_hash=draft.payload_hash,
        )
    )
    assert isinstance(receipt, EffectReceiptV1)
    assert receipt.capability == "gmail.send"
    assert receipt.connection_ref == "google_conn_1234"
    assert receipt.provider_effect_id == receipt.provider_message_id
    assert tuple(receipt.evidence_labels) == ("SENT",)


# ---------------------------------------------------------------------------
# P1 - no dispatch without an exact persisted APPROVED proposal.
# ---------------------------------------------------------------------------
def test_kernel_refuses_dispatch_from_unsaved_proposal():
    record = action()

    class EmptyRepository(Repository):
        def get(self, *, action_id, user_id):
            return None

    repo = EmptyRepository(record)
    adapter, repo, provider = build_adapter(record, repo=repo)

    with pytest.raises(send_module.SendValidationError, match="action does not exist"):
        adapter.dispatch(record)
    assert provider.send_calls == []


# ---------------------------------------------------------------------------
# P2 - no credential resolution before the adapter is admitted.
# ---------------------------------------------------------------------------
class CountingResolver:
    def __init__(self):
        self.calls = 0

    def resolve(self):
        self.calls += 1


class LazyCredsProvider(Provider):
    """Resolves provider credentials only when the dispatch path sends."""

    def __init__(self, resolver, **kwargs):
        super().__init__(**kwargs)
        self._resolver = resolver

    def send_raw(self, **kwargs):
        self._resolver.resolve()
        return super().send_raw(**kwargs)


def _adapter_with_lazy_creds(record, *, founders, deletion_blocked, resolver):
    provider = LazyCredsProvider(resolver)
    return build_adapter(
        record,
        provider=provider,
        founders=founders,
        deletion_blocked=deletion_blocked,
    )


def test_credentials_resolved_only_after_adapter_admitted():
    # (a) non-founder is refused at admission; creds never resolved.
    resolver = CountingResolver()
    record = action()
    adapter, repo, provider = _adapter_with_lazy_creds(
        record, founders={"other"}, deletion_blocked=lambda _u: False, resolver=resolver
    )
    with pytest.raises(models.CapabilityDenied):
        adapter.dispatch(record)
    assert resolver.calls == 0
    assert provider.send_calls == []

    # (b) deletion-fenced action is refused before provider; creds never resolved.
    resolver_b = CountingResolver()
    record_b = action()
    adapter_b, repo_b, provider_b = _adapter_with_lazy_creds(
        record_b, founders={"founder-1"}, deletion_blocked=lambda _u: True, resolver=resolver_b
    )
    with pytest.raises(send_module.EffectUncertain):
        adapter_b.dispatch(record_b)
    assert resolver_b.calls == 0
    assert provider_b.send_calls == []

    # A founder + fence-live APPROVED dispatch resolves creds exactly once.
    resolver_c = CountingResolver()
    record_c = action()
    adapter_c, repo_c, provider_c = _adapter_with_lazy_creds(
        record_c, founders={"founder-1"}, deletion_blocked=lambda _u: False, resolver=resolver_c
    )
    adapter_c.dispatch(record_c)
    assert resolver_c.calls == 1
    assert len(provider_c.send_calls) == 1


# ---------------------------------------------------------------------------
# P3 - exact binding rechecked at dispatch; never sends on mismatch.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutate,founders",
    [
        (lambda r: r, {"other"}),
        (lambda r: r.update(userId="founder-1", approvedActionId="action_other123"), {"founder-1"}),
        (lambda r: r.update(capability="calendar.write"), {"founder-1"}),
        (lambda r: r.update(resource="google:gmail:connection:other_conn_12:account:founder@example.com"), {"founder-1"}),
        (lambda r: r.update(connectionId="other_conn_12"), {"founder-1"}),
        (lambda r: r.update(approvedArgsHash="0" * 64), {"founder-1"}),
        (lambda r: r.update(approvalArgsHash="0" * 64), {"founder-1"}),
        (lambda r: r.update(approvedDraftRevision=5), {"founder-1"}),
        (lambda r: r.update(approvalDraftRevision=5), {"founder-1"}),
        (lambda r: r.update(approvalExpiresAt=r["approvedAt"]), {"founder-1"}),
    ],
)
def test_dispatch_rechecks_full_binding_and_never_sends_on_mismatch(mutate, founders):
    record = action()
    mutate(record)
    adapter, repo, provider = build_adapter(record, founders=founders)
    with pytest.raises((models.CapabilityDenied, send_module.SendValidationError)):
        adapter.dispatch(record)
    assert repo.transitions == []
    assert provider.send_calls == []


# ---------------------------------------------------------------------------
# P4 - deletion fence rechecked immediately before provider dispatch.
# ---------------------------------------------------------------------------
def test_deletion_fence_rechecked_after_claim_before_provider():
    record = action()
    repo = Repository(record)
    states = []

    def deletion_blocked(user_id):
        # False during admission-era, True once the DISPATCHING claim is held.
        blocked = repo.record["state"] == "DISPATCHING"
        states.append((user_id, repo.record["state"]))
        return blocked

    adapter, repo, provider = build_adapter(
        record, repo=repo, deletion_blocked=deletion_blocked
    )

    with pytest.raises(send_module.EffectUncertain, match="account deletion"):
        adapter.dispatch(record)

    assert states == [("founder-1", "DISPATCHING")]
    assert provider.send_calls == []
    assert repo.record["state"] == "UNCERTAIN"
    assert repo.record["uncertaintyReason"] == "account-deletion-fence"


# ---------------------------------------------------------------------------
# P5 - ambiguous provider evidence stays UNCERTAIN and never replays.
# ---------------------------------------------------------------------------
def test_ambiguous_provider_evidence_quarantines_uncertain_without_replay():
    record = action()
    provider = Provider(error=send_module.ProviderEvidenceAmbiguous("ambiguous"))
    adapter, repo, provider = build_adapter(record, provider=provider)

    with pytest.raises(send_module.EffectUncertain):
        adapter.dispatch(record)
    assert repo.record["state"] == "UNCERTAIN"
    assert len(provider.send_calls) == 1

    with pytest.raises(
        send_module.EffectUncertain, match="requires provider reconciliation"
    ):
        adapter.dispatch(repo.record)
    assert len(provider.send_calls) == 1


# ---------------------------------------------------------------------------
# P6 - no resend before reconciliation.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state", [ActionState.DISPATCHING, ActionState.UNCERTAIN]
)
def test_dispatching_or_uncertain_reconciles_never_redispatches(state):
    record = action(state=state, revision=9)

    # dispatch must refuse and never touch the provider.
    adapter, repo, provider = build_adapter(record)
    with pytest.raises(send_module.EffectUncertain):
        adapter.dispatch(record)
    assert provider.send_calls == []

    # Full SENT evidence confirms via reconcile with exactly one find call.
    confirming = Provider(
        found=lambda call: provider_evidence(
            message_id=call["message_id"], payload_hash=call["payload_hash"]
        )
    )
    reconciler, rrepo = gmail_fixtures.reconciler(action(state=state, revision=9), confirming)
    radapter = GmailConnectorAdapter(
        executor=object(), reconciler=reconciler, provider=confirming
    )
    receipt = radapter.reconcile(rrepo.record)
    assert rrepo.record["state"] == "CONFIRMED"
    assert len(confirming.find_calls) == 1
    assert confirming.send_calls == []
    assert receipt is not None

    # Mismatched (wrong payload) evidence leaves the state untouched, returns None.
    mismatched = Provider(
        found=lambda call: provider_evidence(
            message_id=call["message_id"], payload_hash="0" * 64
        )
    )
    reconciler2, rrepo2 = gmail_fixtures.reconciler(action(state=state, revision=9), mismatched)
    radapter2 = GmailConnectorAdapter(
        executor=object(), reconciler=reconciler2, provider=mismatched
    )
    assert radapter2.reconcile(rrepo2.record) is None
    assert rrepo2.record["state"] == state.value
    assert mismatched.send_calls == []


# ---------------------------------------------------------------------------
# P7 - a newer draft revision atomically stales the pending founder approval.
# ---------------------------------------------------------------------------
def test_new_draft_revision_atomically_stales_pending_founder_approval_via_kernel():
    record = state_fixtures.prepared_action(revision=4)
    service, repo = state_fixtures.approval_service(record)
    # Move to a real pending founder approval at draftRevision=4.
    state_fixtures.request_approval(service, record)
    assert repo.record["state"] == "APPROVAL_PENDING"
    pending_revision = repo.record["revision"]

    adapter = GmailConnectorAdapter(executor=object(), approval_service=service)
    stale = adapter.supersede_pending(
        action_id=record["actionId"],
        user_id=record["userId"],
        revision=pending_revision,
        expected_draft_revision=4,
        current_draft_revision=5,
    )
    assert stale["state"] == "STALE"
    assert stale["supersededByDraftRevision"] == 5
    assert repo.record["state"] == "STALE"

    # The old revision can no longer be approved.
    with pytest.raises(state_fixtures.machine_module.ConcurrentActionUpdate):
        service.approve(
            action_id=record["actionId"],
            revision=pending_revision,
            acting_user_id=record["userId"],
            token="x.y",
            args=record["args"],
        )
