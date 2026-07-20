"""Fail-closed clean-account baseline observation for release v2.

The baseline is intentionally independent from mutation acknowledgements.  It
uses one exact account-scoped CloudFormation observer capability and performs
two complete, ordered inventory sweeps before it can report a clean account.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping

from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AwsAuthorityError,
)
from release_tools.contracts import (
    ContractError,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.production_observer_v2 import (
    CanonicalReadObservationV2,
    _new_observation,
)
from release_tools.transaction import ObservationDisposition


REQUIRED_REGION = "eu-west-1"
_BASELINE_STACK_AUTHORITY = (
    "CDKToolkit",
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
    "OpenClawRouter",
    "OpenClawCron",
    "PersonalOperatorScheduler",
    "PersonalOperatorWeb",
    "PersonalOperatorBrowser",
    "PersonalOperatorCompute",
    "OpenClawTokenMonitoring",
)
# Inspection-only detached snapshot.  Baseline authority never reads this
# public binding, so rebinding it cannot weaken the two mandatory sweeps.
BASELINE_STACK_INVENTORY = tuple(name for name in _BASELINE_STACK_AUTHORITY)

_ACCOUNT = re.compile(r"[0-9]{12}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_STACK_STATUS = re.compile(r"[A-Z][A-Z_]{2,63}")
_STACK_ID_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+=,@-]{0,255}")
_REQUEST_FIELDS = frozenset({"schema", "account", "region", "sourceCommit"})


class BaselineObserverV2Error(RuntimeError):
    """The baseline request or observer authority is invalid."""


class BaselineObserverV2Ambiguous(BaselineObserverV2Error):
    """CloudFormation did not yield stable authoritative baseline evidence."""


def _account(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _ACCOUNT.fullmatch(value) is None
        or value == "000000000000"
    ):
        raise BaselineObserverV2Error("baseline account is invalid")
    return value


def _region(value: Any) -> str:
    if value != REQUIRED_REGION:
        raise BaselineObserverV2Error(
            f"baseline region must be exactly {REQUIRED_REGION}"
        )
    return REQUIRED_REGION


def _commit(value: Any) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise BaselineObserverV2Error("baseline source commit is invalid")
    return value


@dataclass(frozen=True, slots=True)
class BaselineObservationRequestV1:
    """Exact canonical subject for one pre-mutation clean-account read."""

    SCHEMA = "personal-operator.baseline-observation-request.v1"

    account: str
    region: str
    source_commit: str

    def __post_init__(self) -> None:
        _account(self.account)
        _region(self.region)
        _commit(self.source_commit)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "BaselineObservationRequestV1":
        if not isinstance(raw, Mapping) or set(raw) != _REQUEST_FIELDS:
            raise BaselineObserverV2Error(
                "baseline observation request has the wrong fields"
            )
        if raw.get("schema") != cls.SCHEMA:
            raise BaselineObserverV2Error(
                "baseline observation request schema is invalid"
            )
        return cls(
            account=_account(raw.get("account")),
            region=_region(raw.get("region")),
            source_commit=_commit(raw.get("sourceCommit")),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "BaselineObservationRequestV1":
        try:
            raw = parse_canonical_object(payload)
        except ContractError as error:
            raise BaselineObserverV2Error(
                "baseline observation request bytes are not canonical"
            ) from error
        return cls.from_mapping(raw)

    def to_mapping(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "account": self.account,
            "region": self.region,
            "sourceCommit": self.source_commit,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _baseline_observation(
    request: BaselineObservationRequestV1,
    *,
    disposition: ObservationDisposition,
    provider_status: str,
    inventory: list[dict[str, str]],
) -> CanonicalReadObservationV2:
    projection = {
        "account": request.account,
        "inventory": inventory,
        "region": request.region,
        "requestSha256": request.digest(),
        "sourceCommit": request.source_commit,
        "sweeps": 2,
    }
    return _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=(
            f"release:{request.account}:{request.region}:"
            f"{request.source_commit}:baseline"
        ),
        disposition=disposition,
        provider_status=provider_status,
        projection=projection,
    )


def _error_details(error: Exception) -> tuple[str, str, int | None]:
    response = getattr(error, "response", None)
    body = response.get("Error") if isinstance(response, Mapping) else None
    metadata = (
        response.get("ResponseMetadata")
        if isinstance(response, Mapping)
        else None
    )
    code = body.get("Code") if isinstance(body, Mapping) else None
    message = body.get("Message") if isinstance(body, Mapping) else None
    status = (
        metadata.get("HTTPStatusCode")
        if isinstance(metadata, Mapping)
        else None
    )
    return (
        code if isinstance(code, str) else "",
        message if isinstance(message, str) else "",
        status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def _is_exact_absence(error: Exception, stack_name: str) -> bool:
    return _error_details(error) == (
        "ValidationError",
        f"Stack with id {stack_name} does not exist",
        400,
    )


class BaselineObserverV2:
    """Run the exact two-sweep CloudFormation baseline observation."""

    def __init__(
        self,
        *,
        account: str,
        region: str,
        cloudformation: object,
    ) -> None:
        self._account = _account(account)
        self._region = _region(region)
        if not isinstance(cloudformation, AttestedAwsClientV2):
            raise BaselineObserverV2Error(
                "baseline observation requires an attested AWS client"
            )
        try:
            cloudformation.require_scope(
                service="cloudformation",
                account=self._account,
                region=self._region,
                capability="observer",
            )
        except AwsAuthorityError as error:
            raise BaselineObserverV2Error(
                "baseline observation client crosses its exact subject"
            ) from error
        self._cloudformation = cloudformation

    def observe(
        self,
        request: BaselineObservationRequestV1,
    ) -> CanonicalReadObservationV2:
        if not isinstance(request, BaselineObservationRequestV1):
            raise BaselineObserverV2Error(
                "baseline observation request is not canonical"
            )
        if (request.account, request.region) != (
            self._account,
            self._region,
        ):
            raise BaselineObserverV2Error(
                "baseline observation request crosses its exact AWS subject"
            )

        first = self._inventory_sweep()
        second = self._inventory_sweep()
        if first != second:
            raise BaselineObserverV2Ambiguous(
                "CloudFormation baseline inventory changed between complete sweeps"
            )
        if any(item["state"] == "PRESENT" for item in first):
            return _baseline_observation(
                request,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status="NONEMPTY_ACCOUNT",
                inventory=first,
            )
        return _baseline_observation(
            request,
            disposition=ObservationDisposition.PRESENT,
            provider_status="CLEAN_ACCOUNT",
            inventory=first,
        )

    def _inventory_sweep(self) -> list[dict[str, str]]:
        return [
            self._describe_stack(name) for name in _BASELINE_STACK_AUTHORITY
        ]

    def _describe_stack(self, stack_name: str) -> dict[str, str]:
        try:
            response = self._cloudformation.invoke(
                "describe_stacks",
                StackName=stack_name,
            )
        except Exception as error:
            if _is_exact_absence(error, stack_name):
                return {"stackName": stack_name, "state": "ABSENT"}
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline read failed for {stack_name}"
            ) from error
        return self._present_projection(response, stack_name)

    def _present_projection(
        self,
        response: object,
        stack_name: str,
    ) -> dict[str, str]:
        if not isinstance(response, Mapping):
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline response is malformed for {stack_name}"
            )
        if response.get("NextToken") not in (None, ""):
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline response is malformed for {stack_name}"
            )
        stacks = response.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline response is malformed for {stack_name}"
            )
        stack = stacks[0]
        if not isinstance(stack, Mapping) or stack.get("StackName") != stack_name:
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline response is malformed for {stack_name}"
            )
        stack_id = stack.get("StackId")
        prefix = (
            f"arn:aws:cloudformation:{self._region}:{self._account}:"
            f"stack/{stack_name}/"
        )
        if (
            not isinstance(stack_id, str)
            or not stack_id.startswith(prefix)
            or _STACK_ID_SUFFIX.fullmatch(stack_id[len(prefix) :]) is None
        ):
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline response is malformed for {stack_name}"
            )
        status = stack.get("StackStatus")
        if not isinstance(status, str) or _STACK_STATUS.fullmatch(status) is None:
            raise BaselineObserverV2Ambiguous(
                f"CloudFormation baseline response is malformed for {stack_name}"
            )
        return {
            "stackId": stack_id,
            "stackName": stack_name,
            "stackStatus": status,
            "state": "PRESENT",
        }


__all__ = [
    "BASELINE_STACK_INVENTORY",
    "BaselineObservationRequestV1",
    "BaselineObserverV2",
    "BaselineObserverV2Ambiguous",
    "BaselineObserverV2Error",
]
