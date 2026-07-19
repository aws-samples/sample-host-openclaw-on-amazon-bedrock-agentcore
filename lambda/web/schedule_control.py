"""Narrow synchronous client for the trusted schedule-control Lambda."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from capabilities.contracts import canonical_json_bytes
from scheduler.control import ControlOutcome


_FUNCTION_ARN = re.compile(
    r"arn:aws:lambda:eu-west-1:[0-9]{12}:function:"
    r"personal-operator-scheduler-control"
)
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_RESPONSE_BYTES = 65_536


class ScheduleControlClientError(RuntimeError):
    pass


def _identities(user_id: str, proposal_ref: str | None = None) -> None:
    if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
        raise ScheduleControlClientError("schedule control user is invalid")
    if proposal_ref is not None and (
        not isinstance(proposal_ref, str)
        or _OPAQUE.fullmatch(proposal_ref) is None
    ):
        raise ScheduleControlClientError("schedule proposal identity is invalid")


def _parse_json(raw: bytes) -> Mapping[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ScheduleControlClientError(
                    "schedule control response is invalid"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _token: (_ for _ in ()).throw(
                ScheduleControlClientError("schedule control response is invalid")
            ),
        )
    except ScheduleControlClientError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ScheduleControlClientError(
            "schedule control response is invalid"
        ) from None
    if not isinstance(value, Mapping):
        raise ScheduleControlClientError("schedule control response is invalid")
    return value


class LambdaScheduleControlClient:
    def __init__(self, *, client: Any, function_arn: str) -> None:
        if not callable(getattr(client, "invoke", None)):
            raise TypeError("schedule control requires a Lambda client")
        if (
            not isinstance(function_arn, str)
            or _FUNCTION_ARN.fullmatch(function_arn) is None
        ):
            raise ValueError("schedule control function ARN is invalid")
        self._client = client
        self._function_arn = function_arn

    def _invoke(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._client.invoke(
            FunctionName=self._function_arn,
            InvocationType="RequestResponse",
            Payload=canonical_json_bytes(request),
        )
        if (
            not isinstance(response, Mapping)
            or response.get("StatusCode") != 200
            or response.get("FunctionError") is not None
        ):
            raise ScheduleControlClientError("schedule control invocation failed")
        payload = response.get("Payload")
        if not callable(getattr(payload, "read", None)):
            raise ScheduleControlClientError("schedule control response is invalid")
        raw = payload.read(_MAX_RESPONSE_BYTES + 1)
        if (
            not isinstance(raw, bytes)
            or not raw
            or len(raw) > _MAX_RESPONSE_BYTES
        ):
            raise ScheduleControlClientError("schedule control response is invalid")
        return _parse_json(raw)

    def preview(self, *, user_id: str, proposal_ref: str) -> dict[str, Any]:
        _identities(user_id, proposal_ref)
        value = self._invoke(
            {
                "action": "PREVIEW",
                "userId": user_id,
                "proposalRef": proposal_ref,
            }
        )
        if set(value) != {
            "proposalRef",
            "operationId",
            "scheduleId",
            "revision",
            "argsHash",
            "arguments",
            "expiresAt",
            "state",
        }:
            raise ScheduleControlClientError("schedule proposal preview is invalid")
        if (
            value.get("proposalRef") != proposal_ref
            or value.get("operationId")
            not in {"schedule.propose", "schedule.cancel.propose"}
            or not isinstance(value.get("scheduleId"), str)
            or _OPAQUE.fullmatch(value["scheduleId"]) is None
            or isinstance(value.get("revision"), bool)
            or not isinstance(value.get("revision"), int)
            or value["revision"] < 1
            or not isinstance(value.get("argsHash"), str)
            or _SHA256.fullmatch(value["argsHash"]) is None
            or not isinstance(value.get("arguments"), Mapping)
            or len(canonical_json_bytes(value["arguments"])) > 8_192
            or isinstance(value.get("expiresAt"), bool)
            or not isinstance(value.get("expiresAt"), int)
            or value.get("state")
            not in {
                "PENDING",
                "APPLYING",
                "SUCCEEDED",
                "UNCERTAIN",
                "REJECTED",
                "STALE",
            }
        ):
            raise ScheduleControlClientError("schedule proposal preview is invalid")
        return dict(value)

    def _decision(
        self,
        action: str,
        *,
        user_id: str,
        proposal_ref: str,
        revision: int,
        args_hash: str,
    ) -> dict[str, Any]:
        _identities(user_id, proposal_ref)
        if (
            action not in {"APPROVE", "REJECT"}
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(args_hash, str)
            or _SHA256.fullmatch(args_hash) is None
        ):
            raise ScheduleControlClientError("schedule decision binding is invalid")
        value = self._invoke(
            {
                "action": action,
                "userId": user_id,
                "proposalRef": proposal_ref,
                "revision": revision,
                "argsHash": args_hash,
            }
        )
        try:
            outcome = ControlOutcome.from_mapping(value)
        except (TypeError, ValueError):
            raise ScheduleControlClientError(
                "schedule control outcome is invalid"
            ) from None
        if outcome.proposal_ref != proposal_ref:
            raise ScheduleControlClientError("schedule outcome crossed its proposal")
        expected = {"SUCCEEDED", "UNCERTAIN"} if action == "APPROVE" else {"REJECTED"}
        if outcome.status not in expected:
            raise ScheduleControlClientError("schedule control outcome is invalid")
        return outcome.to_mapping()

    def approve(self, **kwargs) -> dict[str, Any]:
        return self._decision("APPROVE", **kwargs)

    def reject(self, **kwargs) -> dict[str, Any]:
        return self._decision("REJECT", **kwargs)

    def reconcile(self, *, user_id: str, proposal_ref: str) -> dict[str, Any]:
        _identities(user_id, proposal_ref)
        value = self._invoke(
            {
                "action": "RECONCILE",
                "userId": user_id,
                "proposalRef": proposal_ref,
            }
        )
        try:
            outcome = ControlOutcome.from_mapping(value)
        except (TypeError, ValueError):
            raise ScheduleControlClientError(
                "schedule control outcome is invalid"
            ) from None
        if (
            outcome.proposal_ref != proposal_ref
            or outcome.status not in {"SUCCEEDED", "UNCERTAIN"}
        ):
            raise ScheduleControlClientError("schedule control outcome is invalid")
        return outcome.to_mapping()

    def purge_user_schedules(self, user_id: str) -> int:
        _identities(user_id)
        value = self._invoke({"action": "PURGE_USER", "userId": user_id})
        remaining = value.get("remaining")
        if (
            set(value) != {"remaining"}
            or isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or remaining < 0
            or remaining > 256
        ):
            raise ScheduleControlClientError("schedule purge response is invalid")
        return remaining


__all__ = ["LambdaScheduleControlClient", "ScheduleControlClientError"]
