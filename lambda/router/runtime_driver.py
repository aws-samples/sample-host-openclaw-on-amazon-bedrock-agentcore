"""Trusted RuntimeDriver for one fenced AgentCore session per user."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Mapping

from runtime_state import (
    LeaseLost,
    RuntimeRecord,
    RuntimeState,
    RuntimeUnavailable,
    StaleLease,
    canonical_session_id,
    canonical_user_id,
    generate_session_id,
)


REQUIRED_REGION = "eu-west-1"
_TRACE = re.compile(r"po1_[0-9a-f]{64}")
_GENERATION = re.compile(
    r"g-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ALLOWED_REQUEST_FIELDS = frozenset({"message", "actorId", "channel"})
_FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "sessionId",
        "runtimeSessionId",
        "userId",
        "internalUserId",
        "namespace",
        "leaseOwner",
        "leaseEpoch",
        "invocationId",
    }
)


class RuntimeDriverError(RuntimeError):
    pass


class RuntimeInvocationUncertain(RuntimeDriverError):
    pass


class RuntimeInvocationFailed(RuntimeDriverError):
    pass


class AgentCoreStopUncertain(RuntimeDriverError):
    pass


class AgentCoreAdapter:
    """Small exact-region adapter around the AgentCore data-plane API."""

    MAX_RESPONSE_BYTES = 500_000

    def __init__(
        self,
        client,
        *,
        runtime_arn: str,
        qualifier: str,
        region: str,
    ) -> None:
        if region != REQUIRED_REGION:
            raise RuntimeError(f"runtime region must be exactly {REQUIRED_REGION}")
        client_region = getattr(getattr(client, "meta", None), "region_name", None)
        if client_region and client_region != REQUIRED_REGION:
            raise RuntimeError(
                f"AgentCore client must use exactly {REQUIRED_REGION}; got {client_region}"
            )
        if qualifier != "DEFAULT":
            raise RuntimeError("AgentCore qualifier must be exactly DEFAULT")
        if not runtime_arn or ":eu-west-1:" not in runtime_arn:
            raise RuntimeError("exact eu-west-1 AgentCore runtime ARN is required")
        self.client = client
        self.runtime_arn = runtime_arn
        self.qualifier = qualifier

    @staticmethod
    def _body(response) -> dict:
        body = response.get("response")
        if body is None:
            raise RuntimeInvocationUncertain("AgentCore returned no response body")
        if hasattr(body, "read"):
            raw = body.read(AgentCoreAdapter.MAX_RESPONSE_BYTES + 1)
        elif isinstance(body, bytes):
            raw = body
        else:
            raw = str(body).encode("utf-8")
        if len(raw) > AgentCoreAdapter.MAX_RESPONSE_BYTES:
            raise RuntimeInvocationUncertain("AgentCore response exceeded its bound")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeInvocationUncertain("AgentCore returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise RuntimeInvocationUncertain("AgentCore response must be an object")
        return decoded

    def invoke(
        self,
        *,
        session_id: str,
        user_id: str,
        payload: dict,
        trace_id: str,
    ) -> dict:
        session_id = canonical_session_id(session_id)
        user_id = canonical_user_id(user_id)
        if not isinstance(trace_id, str) or not 1 <= len(trace_id) <= 128:
            raise ValueError("trace_id must be between 1 and 128 characters")
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            qualifier=self.qualifier,
            runtimeSessionId=session_id,
            runtimeUserId=user_id,
            traceId=trace_id,
            payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        status = int(response.get("statusCode", 0))
        returned_session = response.get("runtimeSessionId")
        if returned_session is not None and returned_session != session_id:
            raise RuntimeInvocationUncertain("AgentCore returned another session identity")
        if status < 200 or status >= 300:
            raise RuntimeInvocationUncertain(
                f"AgentCore invocation outcome was HTTP {status or 'unknown'}"
            )
        return self._body(response)

    def stop(self, *, session_id: str) -> dict:
        session_id = canonical_session_id(session_id)
        token = hashlib.sha256(
            (
                "personal-operator-stop-v1\0"
                f"{self.runtime_arn}\0{self.qualifier}\0{session_id}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            response = self.client.stop_runtime_session(
                agentRuntimeArn=self.runtime_arn,
                qualifier=self.qualifier,
                runtimeSessionId=session_id,
                clientToken=token,
            )
        except Exception as error:
            response = getattr(error, "response", None)
            if (
                isinstance(response, dict)
                and response.get("Error", {}).get("Code")
                == "ResourceNotFoundException"
            ):
                return {"stopped": True, "notFound": True}
            raise AgentCoreStopUncertain("AgentCore stop outcome is unknown") from error
        status = int(response.get("statusCode", 0))
        if status < 200 or status >= 300:
            raise AgentCoreStopUncertain(
                f"AgentCore stop outcome was HTTP {status or 'unknown'}"
            )
        returned_session = response.get("runtimeSessionId")
        if returned_session is not None and returned_session != session_id:
            raise AgentCoreStopUncertain("AgentCore stopped another session identity")
        return {"stopped": True, "notFound": False}


class RuntimeDriver:
    def __init__(
        self,
        *,
        repository,
        adapter: AgentCoreAdapter,
        owner_factory=None,
        session_id_factory=None,
        lease_ms: int = 120_000,
        heartbeat_interval_ms: int | None = None,
    ) -> None:
        if lease_ms <= 0:
            raise ValueError("lease duration must be positive")
        self.repository = repository
        self.adapter = adapter
        self.owner_factory = owner_factory or (lambda: f"op-{uuid.uuid4().hex}")
        self.session_id_factory = session_id_factory or generate_session_id
        self.lease_ms = lease_ms
        self.heartbeat_interval_ms = (
            heartbeat_interval_ms
            if heartbeat_interval_ms is not None
            else max(1_000, lease_ms // 3)
        )
        if self.heartbeat_interval_ms <= 0 or self.heartbeat_interval_ms >= lease_ms:
            raise ValueError("heartbeat interval must be positive and below the lease")

    @staticmethod
    def _trace(trace_id: str) -> str:
        value = str(trace_id or "")
        if _TRACE.fullmatch(value) is None:
            raise ValueError("trace_id must be a server-derived po1 identity")
        return value

    @staticmethod
    def _receipt(response: Mapping) -> dict:
        value = response.get("workspaceReceipt")
        if not isinstance(value, Mapping):
            raise RuntimeInvocationUncertain("runtime returned no workspace receipt")
        generation = value.get("generation")
        digest = value.get("manifestSha256")
        if (
            not isinstance(generation, str)
            or _GENERATION.fullmatch(generation) is None
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise RuntimeInvocationUncertain("runtime returned an invalid workspace receipt")
        return {"generation": generation, "manifestSha256": digest}

    def ensure(self, user_id: str) -> RuntimeRecord:
        record = self.repository.ensure(canonical_user_id(user_id))
        if record.tombstoned_at is not None or record.state is RuntimeState.DELETING:
            from runtime_state import TombstonedUser

            raise TombstonedUser(record.user_id)
        if record.state in {RuntimeState.QUARANTINED, RuntimeState.UNHEALTHY}:
            raise RuntimeUnavailable(
                f"runtime is {record.state.value.lower()} and requires recovery"
            )
        return record

    def _quarantine(self, lease: RuntimeRecord) -> None:
        try:
            self.repository.finalize_failure(
                lease, state=RuntimeState.QUARANTINED
            )
        except LeaseLost:
            pass

    def _acquire(self, user_id: str, trace_id: str) -> RuntimeRecord:
        self.ensure(user_id)
        owner = self.owner_factory()
        try:
            return self.repository.acquire(
                user_id,
                owner=owner,
                trace_id=trace_id,
                lease_ms=self.lease_ms,
            )
        except StaleLease as stale_error:
            stale = stale_error.record
            fenced = self.repository.fence_stale(
                stale,
                owner=owner,
                trace_id=trace_id,
                lease_ms=self.lease_ms,
            )
            try:
                self.adapter.stop(session_id=stale.session_id)
            except Exception as error:
                self._quarantine(fenced)
                raise RuntimeInvocationUncertain(
                    "stale runtime could not be proven stopped"
                ) from error
            return self.repository.rotate_after_fence(
                fenced,
                session_id=canonical_session_id(self.session_id_factory()),
                lease_ms=self.lease_ms,
            )

    def _invoke_with_receipt(
        self, lease: RuntimeRecord, *, payload: dict, trace_id: str
    ) -> tuple[dict, dict]:
        try:
            response = self._with_heartbeat(
                lease,
                lambda: self.adapter.invoke(
                    session_id=lease.session_id,
                    user_id=lease.user_id,
                    payload=payload,
                    trace_id=trace_id,
                ),
            )
            status = str(response.get("status", "")).lower()
            if status in {"quarantined", "uncertain", "retryable"}:
                raise RuntimeInvocationUncertain(
                    f"runtime reported {status or 'an uncertain outcome'}"
                )
            receipt = self._receipt(response)
            if status == "failed":
                try:
                    self.repository.finalize_failure(
                        lease, state=RuntimeState.UNHEALTHY
                    )
                except LeaseLost as error:
                    raise RuntimeInvocationUncertain(
                        "runtime failure lost its lease fence"
                    ) from error
                raise RuntimeInvocationFailed("runtime reported a committed failure")
            return response, receipt
        except RuntimeInvocationFailed:
            raise
        except Exception as error:
            self._quarantine(lease)
            if isinstance(error, RuntimeInvocationUncertain):
                raise
            raise RuntimeInvocationUncertain(
                "runtime invocation outcome is unknown and was not retried"
            ) from error

    def _with_heartbeat(self, lease: RuntimeRecord, operation):
        stop = threading.Event()
        failure = []

        def heartbeat_loop():
            interval = self.heartbeat_interval_ms / 1_000
            while not stop.wait(interval):
                try:
                    self.repository.heartbeat(lease, lease_ms=self.lease_ms)
                except Exception as error:
                    failure.append(error)
                    return

        thread = threading.Thread(
            target=heartbeat_loop,
            name="runtime-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        try:
            result = operation()
        finally:
            stop.set()
            thread.join(timeout=max(1.0, self.heartbeat_interval_ms / 1_000 + 0.1))
        if failure:
            raise RuntimeInvocationUncertain("runtime lease heartbeat was lost") from failure[0]
        return result

    def invoke(self, user_id: str, request: Mapping, trace_id: str) -> dict:
        user_id = canonical_user_id(user_id)
        trace_id = self._trace(trace_id)
        if not isinstance(request, Mapping):
            raise ValueError("runtime request must be an object")
        keys = set(request)
        if keys & _FORBIDDEN_REQUEST_FIELDS or keys - _ALLOWED_REQUEST_FIELDS:
            raise ValueError("runtime request contains caller-controlled authority")
        if "message" not in request:
            raise ValueError("runtime request requires a message")
        lease = self._acquire(user_id, trace_id)
        payload = {
            "action": "chat",
            "internalUserId": user_id,
            "namespace": user_id,
            **({"actorId": request["actorId"]} if "actorId" in request else {}),
            **({"channel": request["channel"]} if "channel" in request else {}),
            "message": request["message"],
            "invocationId": trace_id,
        }
        response, receipt = self._invoke_with_receipt(
            lease, payload=payload, trace_id=trace_id
        )
        try:
            self.repository.finalize_success(
                lease, invocation_id=trace_id, receipt=receipt
            )
        except LeaseLost as error:
            raise RuntimeInvocationUncertain(
                "runtime response lost its lease fence and was not acknowledged"
            ) from error
        return response

    def status(self, user_id: str) -> dict:
        user_id = canonical_user_id(user_id)
        current = self.repository.get(user_id)
        if current is None:
            return {
                "userId": user_id,
                "sessionId": None,
                "state": RuntimeState.COLD.value,
                "workspaceReceipt": None,
            }
        return current.public()

    def snapshot(self, user_id: str) -> dict:
        user_id = canonical_user_id(user_id)
        owner_seed = self.owner_factory()
        trace_id = "po1_" + hashlib.sha256(
            f"personal-operator-snapshot-v1\0{user_id}\0{owner_seed}".encode()
        ).hexdigest()
        lease = self._acquire(user_id, trace_id)
        payload = {
            "action": "snapshot",
            "internalUserId": user_id,
            "namespace": user_id,
        }
        _, receipt = self._invoke_with_receipt(
            lease, payload=payload, trace_id=trace_id
        )
        try:
            self.repository.finalize_success(
                lease, invocation_id=trace_id, receipt=receipt
            )
        except LeaseLost as error:
            raise RuntimeInvocationUncertain(
                "snapshot receipt lost its lease fence"
            ) from error
        return receipt

    def stop(self, user_id: str) -> dict:
        user_id = canonical_user_id(user_id)
        current = self.repository.get(user_id)
        if current is None:
            return {
                "userId": user_id,
                "sessionId": None,
                "state": RuntimeState.COLD.value,
            }
        trace_id = "po1_" + hashlib.sha256(
            f"personal-operator-stop-v1\0{user_id}\0{self.owner_factory()}".encode()
        ).hexdigest()
        lease = self._acquire(user_id, trace_id)
        try:
            self.adapter.stop(session_id=lease.session_id)
            result = self.repository.rotate_after_stop(
                lease, session_id=canonical_session_id(self.session_id_factory())
            )
        except Exception as error:
            self._quarantine(lease)
            raise RuntimeInvocationUncertain("runtime stop was not proven") from error
        return result.public()

    def purge(self, user_id: str) -> dict:
        user_id = canonical_user_id(user_id)
        owner = self.owner_factory()
        lease = self.repository.begin_purge(
            user_id, owner=owner, lease_ms=self.lease_ms
        )
        try:
            if lease.session_id:
                self.adapter.stop(session_id=lease.session_id)
            result = self.repository.finish_purge(lease)
        except Exception as error:
            marker = getattr(self.repository, "mark_purge_uncertain", None)
            if marker:
                try:
                    marker(lease)
                except LeaseLost:
                    pass
            raise RuntimeInvocationUncertain("runtime purge stop was not proven") from error
        return result.public()
