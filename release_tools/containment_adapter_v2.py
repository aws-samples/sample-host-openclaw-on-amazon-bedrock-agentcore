"""Production teardown/observation adapter for containment and purge v2.

``release_tools.containment_v2`` is a pure, networkless state contract.  It
mints its capabilities (retained release evidence, reconcilable destructive
observations, single-use action authority) only behind module-private object()
tokens.  This adapter is the production-facing boundary that turns those pure
capabilities into real (here: synthetically faked) mutating and read-only AWS
calls.

Discipline mirrored from the accepted v2 boundary:

* Durable intent before effect.  ``ContainmentJournalV1.arm_next`` appends the
  immutable ``UNCERTAIN`` attempt record and returns a single-use
  ``FreshContainmentAuthorityV1`` before this adapter ever issues an effect.
  The provider spends that authority, then issues exactly one mapped mutating
  API.  A consumed authority cannot re-arm, so a crash after the effect leaves
  the journal ``UNCERTAIN`` with no path to replay.
* Fail closed on ambiguity.  Any uncertain, malformed, or transport-shaped
  provider response raises :class:`ContainmentAdapterUncertain`; the adapter
  never retries a possibly-applied destructive effect.
* Exact identity only.  No wildcard, prefix, or globbed target is ever
  dispatched, and every action identity is checked against the retained
  evidence inventory before an effect is issued.
* Two genuinely separate live reads.  The observer reads the live subject
  twice, requires the two reads to be distinct live reads (a single echoed
  read is rejected), maps live state to a sweep value, and only then mints the
  contract's hash-chained, reconcilable observation through the public seam.

Capability minting note: the pure contract does not export a public factory for
either ``RetainedReleaseEvidenceV1`` or a *reconcilable*
``DestructiveObservationV1``; both are gated behind private tokens.  The only
public seams the contract exposes are
``FakeRetainedReleaseEvidenceBoundaryV1.retain`` and
``FakeContainmentProviderV1.set_sweeps``/``observe_current``.  This adapter does
all authentication, live I/O, and distinctness enforcement itself and then uses
those public seams purely as the token-gated evidence minters.  No private
token is imported and ``containment_v2`` is not modified.
"""

from __future__ import annotations

import abc
import re
from typing import Any, Mapping, Sequence

from release_tools.containment_v2 import (
    CONTAINMENT_RESOURCE_KINDS,
    PURGE_TARGET_KINDS,
    ContainmentError,
    ContainmentJournalV1,
    ContainmentPlanV1,
    DestructiveActionV1,
    DestructiveObservationV1,
    FakeContainmentProviderV1,
    FakeRetainedReleaseEvidenceBoundaryV1,
    FreshContainmentAuthorityV1,
    OwnedResourceIdentityV1,
    PurgePlanV1,
    PurgeTargetV1,
    ReleaseClosureBindingV1,
    RetainedReleaseEvidenceV1,
)


REQUIRED_REGION = "eu-west-1"
_ACCOUNT = re.compile(r"[0-9]{12}")
_S3_VERSION = re.compile(r"s3://([^/?#]+)/([^?#]+)\?versionId=([^&#]+)")
_S3_UPLOAD = re.compile(r"s3://([^/?#]+)/([^?#]+)\?uploadId=([^&#]+)")

# Terminal live-state values the contract accepts for a completed teardown.
_TERMINAL_SWEEP = frozenset({"ABSENT", "SCHEDULED", "CANCELED"})
_SWEEP_VALUES = frozenset({"ABSENT", "PRESENT", "SCHEDULED", "CANCELED", "UNKNOWN"})

# Exact terminal live state required per resource kind.  KMS keys are only ever
# scheduled (never immediately absent); signer profiles are canceled; every
# other kind must read fully absent.
_EXPECTED_TERMINAL = {"KMS_KEY": "SCHEDULED", "SIGNER_SIGNING_PROFILE": "CANCELED"}


def _expected_terminal(resource_kind: str) -> str:
    return _EXPECTED_TERMINAL.get(resource_kind, "ABSENT")


# (provider, operation) -> (sdk service, mutating method).  Providers/operations
# are the exact tokens the contract stamps on each derived DestructiveActionV1.
_TEARDOWN_METHODS: Mapping[tuple[str, str], tuple[str, str]] = {
    ("CLOUDFORMATION", "DELETE_STACK"): ("cloudformation", "delete_stack"),
    ("AGENTCORE_POLICY", "DELETE_RESOURCE_POLICY"): (
        "bedrock-agentcore-control",
        "delete_resource_policy",
    ),
    ("AGENTCORE_CONTROL", "DELETE_ENDPOINT"): (
        "bedrock-agentcore-control",
        "delete_agent_runtime_endpoint",
    ),
    ("AGENTCORE_CONTROL", "DELETE_RUNTIME"): (
        "bedrock-agentcore-control",
        "delete_agent_runtime",
    ),
    ("S3", "ABORT_MULTIPART_UPLOAD"): ("s3", "abort_multipart_upload"),
    ("S3", "DELETE_OBJECT_VERSION"): ("s3", "delete_object"),
    ("S3", "DELETE_BUCKET"): ("s3", "delete_bucket"),
    ("ECR", "BATCH_DELETE_IMAGE"): ("ecr", "batch_delete_image"),
    ("ECR", "DELETE_SIGNING_CONFIGURATION"): ("ecr", "delete_signing_configuration"),
    ("SIGNER", "CANCEL_SIGNING_PROFILE"): ("signer", "cancel_signing_profile"),
    ("ECR", "DELETE_REPOSITORY"): ("ecr", "delete_repository"),
    ("DYNAMODB", "DELETE_TABLE"): ("dynamodb", "delete_table"),
    ("CLOUDWATCH_LOGS", "DELETE_LOG_GROUP"): ("logs", "delete_log_group"),
    ("KMS", "SCHEDULE_KEY_DELETION"): ("kms", "schedule_key_deletion"),
}

# resource kind -> (sdk service, read-only observation method).
_OBSERVE_METHODS: Mapping[str, tuple[str, str]] = {
    "CF_STACK_PERSONAL_OPERATOR_WEB": ("cloudformation", "describe_stacks"),
    "CF_STACK_PERSONAL_OPERATOR_SCHEDULER": ("cloudformation", "describe_stacks"),
    "CF_STACK_OPENCLAW_ROUTER": ("cloudformation", "describe_stacks"),
    "CF_STACK_OPENCLAW_CRON": ("cloudformation", "describe_stacks"),
    "AGENTCORE_ENDPOINT_RESOURCE_POLICY": (
        "bedrock-agentcore-control",
        "get_resource_policy",
    ),
    "AGENTCORE_ENDPOINT": (
        "bedrock-agentcore-control",
        "get_agent_runtime_endpoint",
    ),
    "AGENTCORE_RUNTIME_RESOURCE_POLICY": (
        "bedrock-agentcore-control",
        "get_resource_policy",
    ),
    "AGENTCORE_RUNTIME": ("bedrock-agentcore-control", "get_agent_runtime"),
    "CF_STACK_OPENCLAW_OBSERVABILITY": ("cloudformation", "describe_stacks"),
    "CF_STACK_OPENCLAW_AGENTCORE": ("cloudformation", "describe_stacks"),
    "CF_STACK_PERSONAL_OPERATOR_CAPABILITIES": ("cloudformation", "describe_stacks"),
    "CF_STACK_OPENCLAW_GUARDRAILS": ("cloudformation", "describe_stacks"),
    "CF_STACK_OPENCLAW_VPC": ("cloudformation", "describe_stacks"),
    "CF_STACK_OPENCLAW_SECURITY": ("cloudformation", "describe_stacks"),
    "CF_STACK_CDK_TOOLKIT": ("cloudformation", "describe_stacks"),
    "S3_MULTIPART_UPLOAD": ("s3", "list_multipart_uploads"),
    "S3_OBJECT_VERSION": ("s3", "list_object_versions"),
    "S3_BUCKET": ("s3", "head_bucket"),
    "ECR_IMAGE_REFERENCE": ("ecr", "batch_get_image"),
    "ECR_SIGNING_CONFIGURATION": ("ecr", "get_signing_configuration"),
    "SIGNER_SIGNING_PROFILE": ("signer", "get_signing_profile"),
    "ECR_REPOSITORY": ("ecr", "describe_repositories"),
    "DYNAMODB_TABLE": ("dynamodb", "describe_table"),
    "CLOUDWATCH_LOG_GROUP": ("logs", "describe_log_groups"),
    "KMS_KEY": ("kms", "describe_key"),
}

# S3 identities (bucket, object version, multipart upload) are global; they are
# not structurally account-bound, so every S3 teardown/observation must use an
# account-scoped attested client AND assert ownership at the call boundary.
_S3_KINDS = frozenset(
    {"S3_MULTIPART_UPLOAD", "S3_OBJECT_VERSION", "S3_BUCKET"}
)


class ContainmentAdapterError(RuntimeError):
    """A production teardown request crosses the closed containment boundary."""


class ContainmentAdapterUncertain(ContainmentAdapterError):
    """An effect may have been applied; the release must remain UNCERTAIN."""


def require_exact_teardown_identity(
    identity: object,
    *,
    label: str = "teardown target",
    allow_query: bool = False,
) -> str:
    """Reject any wildcard/prefix/globbed identity; require one exact resource.

    ``allow_query`` permits a single ``?`` query separator, which S3 object
    version and multipart-upload identities legitimately use (``?versionId=`` /
    ``?uploadId=``); it never permits ``*`` glob wildcards or trailing prefixes.
    """

    if (
        not isinstance(identity, str)
        or not identity
        or identity != identity.strip()
        or "\x00" in identity
        or "*" in identity
        or identity.endswith("/")
        or identity.endswith("?")
        or identity.lower().endswith("prefix:")
        or identity in {".", ".."}
    ):
        raise ContainmentAdapterError(f"{label} must be one exact resource")
    if "?" in identity and not allow_query:
        raise ContainmentAdapterError(f"{label} must be one exact resource")
    return identity


class AttestedTeardownClientV1(abc.ABC):
    """Minimal attested-client surface for teardown, mirroring v2 discipline.

    The accepted ``AttestedAwsClientV2`` deliberately scopes its mutation method
    catalog to release/create operations (and excludes KMS/DynamoDB/Signer/Logs
    services), so it cannot dispatch teardown.  Production teardown therefore
    requires a teardown-scoped attested client with the same ``require_scope`` +
    ``invoke`` gating surface; this abstract base defines it, and both a future
    real wrapper and the synthetic test fake implement it.
    """

    @abc.abstractmethod
    def require_scope(
        self, *, service: str, account: str, region: str, capability: str
    ) -> None:
        ...

    @abc.abstractmethod
    def invoke(self, method_name: str, **kwargs: Any) -> object:
        ...


def _validate_client(
    client: object,
    *,
    service: str,
    account: str,
    region: str,
    capability: str,
) -> AttestedTeardownClientV1:
    if not isinstance(client, AttestedTeardownClientV1):
        raise ContainmentAdapterError(
            "production teardown requires an attested teardown client"
        )
    try:
        client.require_scope(
            service=service,
            account=account,
            region=region,
            capability=capability,
        )
    except Exception as error:  # scope errors are provider-defined
        raise ContainmentAdapterError(
            "attested teardown client crosses its exact subject"
        ) from error
    return client


def _validate_account_region(account: object, region: object) -> tuple[str, str]:
    if (
        not isinstance(account, str)
        or _ACCOUNT.fullmatch(account) is None
        or account == "000000000000"
    ):
        raise ContainmentAdapterError("teardown account is invalid")
    if region != REQUIRED_REGION:
        raise ContainmentAdapterError(
            f"teardown region must be exactly {REQUIRED_REGION}"
        )
    return account, region


class ProductionRetainedReleaseEvidenceMinterV1:
    """Authenticate a retained release record, then mint the pure capability.

    This replaces ``FakeRetainedReleaseEvidenceBoundaryV1`` for production: it
    proves the supplied release-evidence digest and account/region bind exactly
    to the inventory before the capability is minted.  It does not import the
    private ``_RETAINED_EVIDENCE_TOKEN``; instead it delegates the final,
    token-gated construction to the contract's public boundary once every check
    has passed.
    """

    @staticmethod
    def mint(
        *,
        binding: ReleaseClosureBindingV1,
        owned_resources: Sequence[OwnedResourceIdentityV1],
        purge_targets: Sequence[PurgeTargetV1],
        release_evidence_sha256: str,
        account: str,
        region: str,
    ) -> RetainedReleaseEvidenceV1:
        if not isinstance(binding, ReleaseClosureBindingV1):
            raise ContainmentAdapterError("retained release binding is invalid")
        account, region = _validate_account_region(account, region)
        if (binding.account, binding.region) != (account, region):
            raise ContainmentAdapterError(
                "retained release binding crosses the exact account or region"
            )
        if release_evidence_sha256 != binding.release_evidence_sha256:
            raise ContainmentAdapterError(
                "retained release evidence digest is not authenticated"
            )
        owned = tuple(owned_resources)
        targets = tuple(purge_targets)
        for resource in owned:
            require_exact_teardown_identity(
                resource.resource_identity, label="retained containment resource"
            )
            _require_account_region_arn(resource.resource_identity, binding)
        for target in targets:
            require_exact_teardown_identity(
                target.resource_identity,
                label="retained purge target",
                allow_query=target.target_kind in _S3_KINDS,
            )
            if target.release_evidence_sha256 != binding.release_evidence_sha256:
                raise ContainmentAdapterError(
                    "retained purge target evidence root is not authenticated"
                )
            _require_account_region_arn(
                target.resource_identity,
                binding,
                allow_non_arn=target.target_kind.startswith("S3_"),
            )
        try:
            return FakeRetainedReleaseEvidenceBoundaryV1.retain(
                binding=binding,
                owned_resources=owned,
                purge_targets=targets,
            )
        except ContainmentError as error:
            raise ContainmentAdapterError(
                "authenticated retained release evidence is not canonical"
            ) from error


def _require_account_region_arn(
    identity: str,
    binding: ReleaseClosureBindingV1,
    *,
    allow_non_arn: bool = False,
) -> None:
    if not identity.startswith("arn:aws:"):
        if allow_non_arn:
            return
        raise ContainmentAdapterError("retained identity must be an exact ARN")
    parts = identity.split(":", 5)
    if len(parts) != 6:
        raise ContainmentAdapterError("retained identity ARN is not exact")
    region, account = parts[3], parts[4]
    if region and region != binding.region:
        raise ContainmentAdapterError("retained identity region crosses the release")
    if account and account != binding.account:
        raise ContainmentAdapterError("retained identity account crosses the release")


def _s3_bucket_of(kind: str, identity: str) -> str:
    if kind == "S3_BUCKET":
        return identity
    if kind == "S3_OBJECT_VERSION":
        match = _S3_VERSION.fullmatch(identity)
    else:
        match = _S3_UPLOAD.fullmatch(identity)
    if match is None:
        raise ContainmentAdapterError("S3 teardown identity is not exact")
    return match.group(1)


def _identity_kwargs(action: DestructiveActionV1) -> dict[str, Any]:
    """Exact identifying request kwargs, shared by write and read paths."""

    identity = action.resource_identity
    kind = action.resource_kind
    if kind in _S3_KINDS:
        kwargs: dict[str, Any] = {"Bucket": _s3_bucket_of(kind, identity)}
        if kind == "S3_OBJECT_VERSION":
            match = _S3_VERSION.fullmatch(identity)
            assert match is not None
            kwargs["Key"] = match.group(2)
            kwargs["VersionId"] = match.group(3)
        elif kind == "S3_MULTIPART_UPLOAD":
            match = _S3_UPLOAD.fullmatch(identity)
            assert match is not None
            kwargs["Key"] = match.group(2)
            kwargs["UploadId"] = match.group(3)
        return kwargs
    if kind == "KMS_KEY":
        return {"KeyId": identity}
    return {"ResourceIdentity": identity}


def _ownership_kwargs(action: DestructiveActionV1, account: str) -> dict[str, Any]:
    """Account-ownership assertion required for non-account-bound S3 targets."""

    if action.resource_kind in _S3_KINDS:
        return {"ExpectedBucketOwner": account}
    return {"ExpectedAccount": account}


class ProductionDestructiveProviderV1:
    """Issue exactly one mapped mutating API per plan step, fail-closed.

    It satisfies the same dispatch surface the pure ``FakeContainmentProviderV1``
    exposes (``dispatch`` / ``dispatch_count``), so it plugs into the contract's
    arm/dispatch/observe/reconcile loop unchanged, but it issues real (here:
    synthetically faked) destructive effects through injected attested clients.
    """

    def __init__(
        self,
        plan: ContainmentPlanV1 | PurgePlanV1,
        *,
        clients: Mapping[str, object],
        account: str,
        region: str,
        retained_evidence: RetainedReleaseEvidenceV1,
    ) -> None:
        if not isinstance(plan, (ContainmentPlanV1, PurgePlanV1)):
            raise ContainmentAdapterError("destructive plan type is invalid")
        parsed = type(plan).from_bytes(plan.to_bytes())
        if parsed != plan or not plan._authorized:
            raise ContainmentAdapterError(
                "destructive provider requires a retained plan capability"
            )
        if not isinstance(retained_evidence, RetainedReleaseEvidenceV1):
            raise ContainmentAdapterError(
                "destructive provider requires retained release evidence"
            )
        self._account, self._region = _validate_account_region(account, region)
        if (plan.binding.account, plan.binding.region) != (
            self._account,
            self._region,
        ):
            raise ContainmentAdapterError(
                "destructive plan crosses the exact account or region"
            )
        self._plan = plan
        self._clients = dict(clients)
        # Every effect identity must be present in the retained inventory.
        self._retained_identities = {
            resource.resource_identity
            for resource in retained_evidence.owned_resources
        } | {target.resource_identity for target in retained_evidence.purge_targets}
        self._counts: dict[str, int] = {
            action.digest(): 0 for action in plan.actions
        }

    def _action(self, action: DestructiveActionV1) -> DestructiveActionV1:
        canonical = DestructiveActionV1.from_mapping(action.to_mapping())
        if (
            canonical.ordinal >= len(self._plan.actions)
            or self._plan.actions[canonical.ordinal] != canonical
        ):
            raise ContainmentAdapterError("destructive action differs from its plan")
        return canonical

    def _client(self, service: str, account: str) -> AttestedTeardownClientV1:
        return _validate_client(
            self._clients.get(service),
            service=service,
            account=account,
            region=self._region,
            capability="mutation",
        )

    def _request_kwargs(
        self, action: DestructiveActionV1, service: str
    ) -> dict[str, Any]:
        kwargs = dict(_identity_kwargs(action))
        kwargs.update(_ownership_kwargs(action, self._account))
        if action.resource_kind == "KMS_KEY":
            kwargs["PendingWindowInDays"] = 30
        return kwargs

    def dispatch(
        self,
        authority: FreshContainmentAuthorityV1,
        action: DestructiveActionV1,
        *,
        crash_before_effect: bool = False,
        crash_after_effect: bool = False,
    ) -> Any:
        if not isinstance(authority, FreshContainmentAuthorityV1):
            raise ContainmentAdapterError(
                "destructive dispatch requires fresh containment authority"
            )
        canonical = self._action(action)
        require_exact_teardown_identity(
            canonical.resource_identity,
            allow_query=canonical.resource_kind in _S3_KINDS,
        )
        if canonical.resource_identity not in self._retained_identities:
            raise ContainmentAdapterError(
                "destructive action identity is not in the retained inventory"
            )
        try:
            service, method = _TEARDOWN_METHODS[
                (canonical.provider, canonical.operation)
            ]
        except KeyError as error:
            raise ContainmentAdapterError(
                "destructive action has no mapped teardown API"
            ) from error
        if canonical.resource_kind in _S3_KINDS and service != "s3":
            raise ContainmentAdapterError("S3 teardown must use the S3 service")
        client = self._client(service, self._account)
        # The journal already appended the durable UNCERTAIN attempt before this
        # call; consuming the single-use authority spends it exactly once.
        attempt = authority.consume(canonical)
        self._counts[canonical.digest()] += 1
        if crash_before_effect:
            raise RuntimeError("simulated crash before destructive effect")
        kwargs = self._request_kwargs(canonical, service)
        try:
            response = client.invoke(method, **kwargs)
        except Exception as error:
            raise ContainmentAdapterUncertain(
                "destructive effect has an unknown outcome; release remains UNCERTAIN"
            ) from error
        if not isinstance(response, Mapping) or response.get("acknowledged") is not True:
            raise ContainmentAdapterUncertain(
                "destructive effect was not acknowledged; release remains UNCERTAIN"
            )
        if crash_after_effect:
            raise RuntimeError("simulated crash after destructive effect")
        return attempt

    def dispatch_count(self, action: DestructiveActionV1) -> int:
        return self._counts[self._action(action).digest()]


class ProductionTeardownObserverV1:
    """Two-sweep live observer that mints the contract's reconcilable evidence.

    It performs two genuinely separate live reads of the exact subject, requires
    them to be distinct live reads (a single echoed read is rejected), maps live
    state to a sweep value bounded by the resource kind, and then mints the
    hash-chained, reconcilable ``DestructiveObservationV1`` through the pure
    contract's public observation seam.
    """

    def __init__(
        self,
        plan: ContainmentPlanV1 | PurgePlanV1,
        *,
        clients: Mapping[str, object],
        account: str,
        region: str,
    ) -> None:
        if not isinstance(plan, (ContainmentPlanV1, PurgePlanV1)):
            raise ContainmentAdapterError("destructive plan type is invalid")
        self._account, self._region = _validate_account_region(account, region)
        self._plan = plan
        self._clients = dict(clients)
        # The contract only mints a *reconcilable* observation through this
        # simulator's token-gated seam; we drive it with our own live values.
        self._minter = FakeContainmentProviderV1.from_plan(plan)

    def _client(self, service: str, account: str) -> AttestedTeardownClientV1:
        return _validate_client(
            self._clients.get(service),
            service=service,
            account=account,
            region=self._region,
            capability="observer",
        )

    def _live_read(
        self, service: str, method: str, action: DestructiveActionV1
    ) -> tuple[str, int]:
        account = self._account
        client = self._client(service, account)
        kwargs = dict(_identity_kwargs(action))
        kwargs.update(_ownership_kwargs(action, account))
        try:
            response = client.invoke(method, **kwargs)
        except Exception as error:
            raise ContainmentAdapterUncertain(
                "teardown observation read failed without authoritative evidence"
            ) from error
        if not isinstance(response, Mapping):
            raise ContainmentAdapterUncertain(
                "teardown observation read is malformed"
            )
        state = response.get("liveState")
        sequence = response.get("readSequence")
        if (
            state not in _SWEEP_VALUES
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise ContainmentAdapterUncertain(
                "teardown observation read is not a canonical live state"
            )
        return state, sequence

    def observe_current(
        self, journal: ContainmentJournalV1
    ) -> DestructiveObservationV1:
        if not isinstance(journal, ContainmentJournalV1):
            raise ContainmentAdapterError("teardown observer journal is invalid")
        if journal.plan.digest() != self._plan.digest():
            raise ContainmentAdapterError("teardown observer plan differs")
        attempt = journal.current_attempt
        if journal.state != "UNCERTAIN" or attempt is None:
            raise ContainmentAdapterError(
                "teardown observer requires one UNCERTAIN action"
            )
        action = self._plan.actions[journal.cursor]
        service, method = _OBSERVE_METHODS[action.resource_kind]
        sweep_one, sequence_one = self._live_read(service, method, action)
        sweep_two, sequence_two = self._live_read(service, method, action)
        if sequence_one == sequence_two:
            raise ContainmentAdapterUncertain(
                "teardown observation reused a single echoed read"
            )
        # Mint the contract's reconcilable, hash-chained observation.
        self._minter.set_sweeps(action, sweep_one, sweep_two)
        return self._minter.observe_current(journal)


__all__ = [
    "AttestedTeardownClientV1",
    "ContainmentAdapterError",
    "ContainmentAdapterUncertain",
    "ProductionDestructiveProviderV1",
    "ProductionRetainedReleaseEvidenceMinterV1",
    "ProductionTeardownObserverV1",
    "require_exact_teardown_identity",
    "REQUIRED_REGION",
]
