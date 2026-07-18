"""Trusted RuntimeDriver for one fenced AgentCore session per user."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Mapping

try:
    from .runtime_state import (
        DuplicateTraceUncertain,
        LeaseLost,
        RuntimeRecord,
        RuntimeState,
        RuntimeStateError,
        RuntimeUnavailable,
        StaleLease,
        TombstonedUser,
        canonical_session_id,
        canonical_runtime_arn,
        canonical_runtime_qualifier,
        canonical_user_id,
        generate_session_id,
        runtime_lineage,
    )
except ImportError:  # direct router Lambda asset and focused tests
    from runtime_state import (
        DuplicateTraceUncertain,
        LeaseLost,
        RuntimeRecord,
        RuntimeState,
        RuntimeStateError,
        RuntimeUnavailable,
        StaleLease,
        TombstonedUser,
        canonical_session_id,
        canonical_runtime_arn,
        canonical_runtime_qualifier,
        canonical_user_id,
        generate_session_id,
        runtime_lineage,
    )


REQUIRED_REGION = "eu-west-1"
_TRACE = re.compile(r"po1_[0-9a-f]{64}")
_GENERATION = re.compile(
    r"g-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPERATION_ID = re.compile(r"op_[0-9a-f]{64}")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
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


class AgentCoreStopUncertain(RuntimeDriverError):
    pass


class AgentCoreAdapter:
    """Small exact-region adapter around the AgentCore data-plane API."""

    MAX_RESPONSE_BYTES = 500_000

    @staticmethod
    def _unique_json_object(pairs) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(_value):
        raise ValueError("non-finite JSON number")

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
        try:
            runtime_arn = canonical_runtime_arn(runtime_arn)
            qualifier = canonical_runtime_qualifier(qualifier)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
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
            decoded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=AgentCoreAdapter._unique_json_object,
                parse_constant=AgentCoreAdapter._reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
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
        if returned_session != session_id:
            raise RuntimeInvocationUncertain("AgentCore returned another session identity")
        if status != 200:
            raise RuntimeInvocationUncertain(
                f"AgentCore invocation outcome was HTTP {status or 'unknown'}"
            )
        return self._body(response)

    def stop(
        self,
        *,
        session_id: str,
        operation_id: str,
        runtime_arn: str | None = None,
        qualifier: str | None = None,
    ) -> dict:
        session_id = canonical_session_id(session_id)
        operation_id = str(operation_id or "")
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise ValueError("stop operation_id must be a durable op identity")
        target_runtime_arn = canonical_runtime_arn(runtime_arn or self.runtime_arn)
        target_qualifier = canonical_runtime_qualifier(qualifier or self.qualifier)
        if runtime_lineage(target_runtime_arn) != runtime_lineage(self.runtime_arn):
            raise AgentCoreStopUncertain("recorded runtime ARN is outside configured lineage")
        token = hashlib.sha256(
            (
                "personal-operator-stop-v1\0"
                f"{target_runtime_arn}\0{target_qualifier}\0{session_id}\0"
                f"{operation_id}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            response = self.client.stop_runtime_session(
                agentRuntimeArn=target_runtime_arn,
                qualifier=target_qualifier,
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
        if status != 200:
            raise AgentCoreStopUncertain(
                f"AgentCore stop outcome was HTTP {status or 'unknown'}"
            )
        returned_session = response.get("runtimeSessionId")
        if returned_session != session_id:
            raise AgentCoreStopUncertain("AgentCore stopped another session identity")
        return {"stopped": True, "notFound": False}


class RuntimeDriver:
    MAX_REQUEST_BYTES = 131_072
    MAX_MESSAGE_BYTES = 131_072
    MAX_CHAT_RESPONSE_BYTES = 100_000
    MAX_ACTOR_ID_BYTES = 256
    ALLOWED_CHANNELS = frozenset({"telegram", "slack", "feishu"})
    ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
        {"image/jpeg", "image/png", "image/gif", "image/webp"}
    )

    def __init__(
        self,
        *,
        repository,
        adapter: AgentCoreAdapter,
        owner_factory=None,
        session_id_factory=None,
        lease_ms: int = 120_000,
        max_execution_ms: int,
        heartbeat_interval_ms: int | None = None,
    ) -> None:
        if max_execution_ms <= 0:
            raise ValueError("maximum execution authority must be positive")
        if lease_ms <= max_execution_ms:
            raise ValueError("lease duration must outlive maximum execution authority")
        if (
            canonical_runtime_arn(repository.runtime_arn)
            != canonical_runtime_arn(adapter.runtime_arn)
            or canonical_runtime_qualifier(repository.runtime_qualifier)
            != canonical_runtime_qualifier(adapter.qualifier)
        ):
            raise ValueError("repository and AgentCore runtime binding disagree")
        self.repository = repository
        self.adapter = adapter
        self.owner_factory = owner_factory or (lambda: f"op-{uuid.uuid4().hex}")
        self.session_id_factory = session_id_factory or generate_session_id
        self.lease_ms = lease_ms
        self.max_execution_ms = max_execution_ms
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
        if not isinstance(value, Mapping) or set(value) != {
            "generation",
            "manifestSha256",
        }:
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

    @staticmethod
    def _bounded_string(value, *, name: str, maximum: int, allow_empty=False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if not allow_empty and not value:
            raise ValueError(f"{name} must not be empty")
        if len(value.encode("utf-8")) > maximum:
            raise ValueError(f"{name} exceeded its UTF-8 bound")
        return value

    @classmethod
    def _validated_message(cls, user_id: str, message):
        if isinstance(message, str):
            return cls._bounded_string(
                message,
                name="message",
                maximum=cls.MAX_MESSAGE_BYTES,
                allow_empty=True,
            )
        if not isinstance(message, Mapping) or set(message) != {"text", "images"}:
            raise ValueError("structured message has an invalid shape")
        text = cls._bounded_string(
            message["text"],
            name="structured message text",
            maximum=cls.MAX_MESSAGE_BYTES,
            allow_empty=True,
        )
        images = message["images"]
        if not isinstance(images, list) or len(images) != 1:
            raise ValueError("structured message requires exactly one image")
        image = images[0]
        if not isinstance(image, Mapping) or set(image) != {"s3Key", "contentType"}:
            raise ValueError("structured image has an invalid shape")
        key = cls._bounded_string(
            image["s3Key"], name="image key", maximum=1_024
        )
        if not key.startswith(f"{user_id}/_uploads/") or any(
            segment in {"", ".", ".."} for segment in key.split("/")
        ):
            raise ValueError("image key is outside the server user namespace")
        content_type = cls._bounded_string(
            image["contentType"], name="image content type", maximum=64
        )
        if content_type not in cls.ALLOWED_IMAGE_CONTENT_TYPES:
            raise ValueError("image content type is not allowed")
        return {
            "text": text,
            "images": [{"s3Key": key, "contentType": content_type}],
        }

    @classmethod
    def _validated_request(cls, user_id: str, request: Mapping) -> dict:
        if not isinstance(request, Mapping):
            raise ValueError("runtime request must be an object")
        keys = set(request)
        if keys & _FORBIDDEN_REQUEST_FIELDS or keys - _ALLOWED_REQUEST_FIELDS:
            raise ValueError("runtime request contains caller-controlled authority")
        if "message" not in request:
            raise ValueError("runtime request requires a message")
        validated = {"message": cls._validated_message(user_id, request["message"])}
        if "actorId" in request:
            validated["actorId"] = cls._bounded_string(
                request["actorId"],
                name="actorId",
                maximum=cls.MAX_ACTOR_ID_BYTES,
            )
        if "channel" in request:
            channel = cls._bounded_string(
                request["channel"], name="channel", maximum=16
            )
            if channel not in cls.ALLOWED_CHANNELS:
                raise ValueError("unsupported runtime channel")
            validated["channel"] = channel
        try:
            encoded = json.dumps(
                validated,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("runtime request is not canonical JSON") from error
        if len(encoded) > cls.MAX_REQUEST_BYTES:
            raise ValueError("runtime request exceeded its JSON bound")
        return validated

    @classmethod
    def _validate_payload_json_bound(cls, payload: Mapping) -> None:
        try:
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("runtime payload is not canonical JSON") from error
        if len(encoded) > cls.MAX_REQUEST_BYTES:
            raise ValueError("runtime payload exceeded its JSON bound")

    @classmethod
    def _validated_outcome(
        cls, response: Mapping, *, action: str, user_id: str
    ) -> dict:
        if not isinstance(response, Mapping):
            raise RuntimeInvocationUncertain("runtime response must be an object")
        if response.get("internalUserId") != user_id:
            raise RuntimeInvocationUncertain("runtime returned another internal identity")
        status = response.get("status")
        if action == "snapshot":
            if set(response) != {
                "internalUserId",
                "status",
                "workspaceReceipt",
            }:
                raise RuntimeInvocationUncertain(
                    "runtime returned an invalid snapshot response shape"
                )
            if status != "snapshotted":
                raise RuntimeInvocationUncertain("runtime returned the wrong snapshot status")
        elif action == "chat":
            required = {
                "internalUserId",
                "status",
                "response",
                "workspaceReceipt",
            }
            keys = set(response)
            if not required.issubset(keys) or keys - (required | {"errorCode"}):
                raise RuntimeInvocationUncertain(
                    "runtime returned an invalid chat response shape"
                )
            if status not in {"ok", "failed"}:
                raise RuntimeInvocationUncertain("runtime returned the wrong chat status")
            error_code = response.get("errorCode")
            if (
                (status == "ok" and "errorCode" in response)
                or (
                    "errorCode" in response
                    and (
                        not isinstance(error_code, str)
                        or _ERROR_CODE.fullmatch(error_code) is None
                    )
                )
            ):
                raise RuntimeInvocationUncertain(
                    "runtime returned invalid error metadata"
                )
            text = response.get("response")
            if not isinstance(text, str) or len(text.encode("utf-8")) > cls.MAX_CHAT_RESPONSE_BYTES:
                raise RuntimeInvocationUncertain("runtime returned an invalid chat response")
        else:
            raise RuntimeInvocationUncertain("runtime returned for an unknown action")
        return dict(response)

    def ensure(self, user_id: str) -> RuntimeRecord:
        record = self.repository.ensure(canonical_user_id(user_id))
        if record.tombstoned_at is not None or record.state is RuntimeState.DELETING:
            raise TombstonedUser(record.user_id)
        if record.state in {RuntimeState.QUARANTINED, RuntimeState.UNHEALTHY}:
            raise RuntimeUnavailable(
                f"runtime is {record.state.value.lower()} and requires recovery"
            )
        if not self.repository.binding_matches(record):
            return self._recover_binding(record)
        return record

    def _quarantine(self, lease: RuntimeRecord) -> None:
        try:
            quarantine = getattr(self.repository, "quarantine", None)
            if quarantine:
                quarantine(lease)
            else:
                self.repository.finalize_failure(
                    lease, state=RuntimeState.QUARANTINED
                )
        except (LeaseLost, RuntimeStateError):
            pass

    def _synchronized_stop(self, lease: RuntimeRecord) -> dict:
        try:
            lease = self.repository.heartbeat(lease, lease_ms=self.lease_ms)
        except Exception as error:
            raise RuntimeInvocationUncertain(
                "runtime stop fence could not be synchronized"
            ) from error
        if not lease.stop_operation_id:
            raise RuntimeInvocationUncertain(
                "runtime stop has no durable operation identity"
            )
        return self.adapter.stop(
            session_id=lease.session_id,
            operation_id=lease.stop_operation_id,
            runtime_arn=lease.runtime_arn,
            qualifier=lease.runtime_qualifier,
        )

    def _recover_binding(self, record: RuntimeRecord) -> RuntimeRecord:
        if runtime_lineage(record.runtime_arn) != runtime_lineage(
            self.adapter.runtime_arn
        ):
            self._quarantine(record)
            raise RuntimeUnavailable(
                "recorded runtime ARN is outside the configured lineage"
            )
        owner = self.owner_factory()
        trace_id = "po1_" + hashlib.sha256(
            (
                "personal-operator-binding-recovery-v1\0"
                f"{record.user_id}\0{record.runtime_arn}\0"
                f"{self.adapter.runtime_arn}\0{record.lease_epoch}"
            ).encode("utf-8")
        ).hexdigest()
        fenced = self.repository.fence_binding_mismatch(
            record,
            owner=owner,
            trace_id=trace_id,
            lease_ms=self.lease_ms,
        )
        try:
            self._synchronized_stop(fenced)
            return self.repository.rotate_binding(
                fenced,
                session_id=canonical_session_id(self.session_id_factory()),
            )
        except Exception as error:
            self._quarantine(fenced)
            if isinstance(error, RuntimeUnavailable):
                raise
            raise RuntimeInvocationUncertain(
                "recorded runtime version could not be safely replaced"
            ) from error

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
        except DuplicateTraceUncertain as duplicate_error:
            self._quarantine(duplicate_error.record)
            raise RuntimeInvocationUncertain(
                "duplicate trace cannot be reexecuted without a durable result ledger"
            ) from duplicate_error
        except StaleLease as stale_error:
            stale = stale_error.record
            if stale.last_trace_id == trace_id:
                self._quarantine(stale)
                raise RuntimeInvocationUncertain(
                    "stale duplicate trace cannot be safely reexecuted"
                ) from stale_error
            fenced = self.repository.fence_stale(
                stale,
                owner=owner,
                trace_id=trace_id,
                lease_ms=self.lease_ms,
            )
            try:
                self._synchronized_stop(fenced)
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
            response = self._validated_outcome(
                response,
                action=payload.get("action", ""),
                user_id=lease.user_id,
            )
            receipt = self._receipt(response)
            return response, receipt
        except Exception as error:
            self._quarantine(lease)
            if isinstance(error, RuntimeInvocationUncertain):
                raise
            raise RuntimeInvocationUncertain(
                "runtime invocation outcome is unknown and was not retried"
            ) from error

    def _with_heartbeat(self, lease: RuntimeRecord, operation):
        try:
            active_lease = self.repository.heartbeat(
                lease, lease_ms=self.lease_ms
            )
        except Exception as error:
            raise RuntimeInvocationUncertain(
                "runtime lease fence could not be synchronized"
            ) from error
        stop = threading.Event()
        failure = []

        def heartbeat_loop():
            interval = self.heartbeat_interval_ms / 1_000
            while not stop.wait(interval):
                try:
                    self.repository.heartbeat(active_lease, lease_ms=self.lease_ms)
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
        request = self._validated_request(user_id, request)
        payload = {
            "action": "chat",
            "internalUserId": user_id,
            "namespace": user_id,
            **({"actorId": request["actorId"]} if "actorId" in request else {}),
            **({"channel": request["channel"]} if "channel" in request else {}),
            "message": request["message"],
            "invocationId": trace_id,
        }
        self._validate_payload_json_bound(payload)
        lease = self._acquire(user_id, trace_id)
        response, receipt = self._invoke_with_receipt(
            lease, payload=payload, trace_id=trace_id
        )
        try:
            self.repository.finalize_success(
                lease, invocation_id=trace_id, receipt=receipt
            )
        except (LeaseLost, RuntimeStateError) as error:
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
        except (LeaseLost, RuntimeStateError) as error:
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
        if not self.repository.binding_matches(current):
            current = self._recover_binding(current)
        owner = self.owner_factory()
        trace_id = "po1_" + hashlib.sha256(
            f"personal-operator-stop-v2\0{user_id}\0{owner}".encode()
        ).hexdigest()
        lease = self.repository.begin_stop(
            current,
            owner=owner,
            trace_id=trace_id,
            lease_ms=self.lease_ms,
        )
        try:
            self._synchronized_stop(lease)
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
                self._synchronized_stop(lease)
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
