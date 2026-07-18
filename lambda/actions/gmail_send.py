"""Founder-only, exact-payload Gmail effect execution with evidence fencing."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP, default
import hashlib
import hmac
import re
from typing import Iterable, Mapping

try:
    from .models import (
        ActionState,
        CapabilityDenied,
        EffectReceipt,
        WaitingForReply,
        canonical_args_hash,
        gmail_resource,
    )
except ImportError:
    from action_models import (
        ActionState,
        CapabilityDenied,
        EffectReceipt,
        WaitingForReply,
        canonical_args_hash,
        gmail_resource,
    )


class SendValidationError(ValueError):
    pass


class EffectUncertain(RuntimeError):
    pass


class ProviderEvidenceAmbiguous(RuntimeError):
    pass


_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
_ACTION = re.compile(r"[A-Za-z0-9_-]{8,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MESSAGE_ID = re.compile(r"<po-[0-9a-f]{24}@personal-operator\.invalid>")


def validate_email_args(args: object) -> dict[str, str]:
    if not isinstance(args, Mapping) or set(args) != {"to", "subject", "body"}:
        raise SendValidationError("Gmail send accepts only to, subject, and body")
    recipient = args["to"]
    subject = args["subject"]
    body = args["body"]
    if not isinstance(recipient, str) or _EMAIL.fullmatch(recipient) is None:
        raise SendValidationError("recipient must be one canonical email address")
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 200
        or "\r" in subject
        or "\n" in subject
    ):
        raise SendValidationError("subject is invalid")
    if not isinstance(body, str) or not body or len(body) > 20_000 or "\x00" in body:
        raise SendValidationError("body must be bounded plain text")
    return {"to": recipient, "subject": subject, "body": body}


def deterministic_message_id(
    *,
    action_id: str,
    draft_revision: int,
    resource: str,
    payload_hash: str,
) -> str:
    if not isinstance(action_id, str) or _ACTION.fullmatch(action_id) is None:
        raise SendValidationError("action ID is invalid")
    if isinstance(draft_revision, bool) or not isinstance(draft_revision, int) or draft_revision < 1:
        raise SendValidationError("draft revision is invalid")
    if not isinstance(resource, str) or not resource or len(resource) > 512 or "\x00" in resource:
        raise SendValidationError("resource is invalid")
    if not isinstance(payload_hash, str) or _SHA256.fullmatch(payload_hash) is None:
        raise SendValidationError("payload hash is invalid")
    digest = hashlib.sha256(
        (
            "personal-operator-gmail-v2\0"
            f"{action_id}\0{draft_revision}\0{resource}\0{payload_hash}"
        ).encode()
    ).hexdigest()[:24]
    return f"<po-{digest}@personal-operator.invalid>"


def _single_header(message, name: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1 or not isinstance(values[0], str):
        raise SendValidationError(f"raw Gmail message has invalid {name}")
    return values[0]


def _parse_exact_raw(raw: bytes, *, expected_message_id: str) -> tuple[str, dict[str, str]]:
    if not isinstance(raw, bytes) or not raw or len(raw) > 64 * 1024:
        raise SendValidationError("raw Gmail message is invalid or too large")
    try:
        message = BytesParser(policy=default).parsebytes(raw)
    except Exception as error:
        raise SendValidationError("raw Gmail message is malformed") from error
    if message.defects or message.is_multipart() or message.get_content_type() != "text/plain":
        raise SendValidationError("raw Gmail message must be one plain-text part")
    if message.get_all("Cc", []) or message.get_all("Bcc", []):
        raise SendValidationError("raw Gmail message contains forbidden recipients")
    message_id = _single_header(message, "Message-ID")
    if not hmac.compare_digest(message_id, expected_message_id):
        raise SendValidationError("raw Gmail message is not bound to Message-ID")
    sender = _single_header(message, "From")
    recipient = _single_header(message, "To")
    subject = _single_header(message, "Subject")
    try:
        body = message.get_content().rstrip("\r\n")
    except Exception as error:
        raise SendValidationError("raw Gmail body cannot be decoded") from error
    return sender, validate_email_args({"to": recipient, "subject": subject, "body": body})


def _decode_provider_raw(value: object) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 100_000:
        raise ProviderEvidenceAmbiguous("Gmail raw evidence is missing")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise ProviderEvidenceAmbiguous("Gmail raw evidence is malformed") from error


class GmailApiAdapter:
    """Gmail API boundary returning only fully verified SENT evidence."""

    def __init__(
        self,
        service,
        *,
        connection_id: str,
        account_email: str,
        user_id: str = "me",
    ) -> None:
        if service is None:
            raise ValueError("Gmail service is required")
        self._resource = gmail_resource(
            connection_id=connection_id, account_email=account_email
        )
        self._connection_id = connection_id
        self._account_email = account_email
        if not isinstance(user_id, str) or not user_id or len(user_id) > 320 or "\x00" in user_id:
            raise ValueError("Gmail user_id is invalid")
        self._service = service
        self._user_id = user_id

    @staticmethod
    def _message_id(value: str) -> str:
        if not isinstance(value, str) or _MESSAGE_ID.fullmatch(value) is None:
            raise SendValidationError("deterministic Message-ID is invalid")
        return value

    def _messages(self):
        try:
            return self._service.users().messages()
        except Exception as error:
            raise ProviderEvidenceAmbiguous("Gmail API boundary is unavailable") from error

    def _assert_bound_provider_account(self) -> None:
        """Prove the OAuth connection currently resolves to the approved account."""

        try:
            response = (
                self._service.users()
                .getProfile(userId=self._user_id)
                .execute()
            )
        except Exception as error:
            raise ProviderEvidenceAmbiguous("Gmail account binding is unavailable") from error
        if (
            not isinstance(response, Mapping)
            or response.get("emailAddress") != self._account_email
        ):
            raise ProviderEvidenceAmbiguous(
                "Gmail OAuth connection is not bound to the approved account"
            )

    def _fetch_exact_evidence(
        self,
        *,
        provider_id: str,
        message_id: str,
        sender_address: str,
        recipient: str,
        payload_hash: str,
    ) -> dict[str, object]:
        try:
            evidence = (
                self._messages()
                .get(userId=self._user_id, id=provider_id, format="raw")
                .execute()
            )
        except Exception as error:
            raise ProviderEvidenceAmbiguous("Gmail evidence lookup is unavailable") from error
        if not isinstance(evidence, Mapping):
            raise ProviderEvidenceAmbiguous("Gmail evidence response is invalid")
        evidence_id = evidence.get("id")
        thread_id = evidence.get("threadId")
        labels = evidence.get("labelIds")
        internal_date = evidence.get("internalDate")
        if (
            evidence_id != provider_id
            or not isinstance(thread_id, str)
            or not thread_id
            or not isinstance(labels, list)
            or "SENT" not in labels
            or not isinstance(internal_date, str)
            or not internal_date.isdecimal()
        ):
            raise ProviderEvidenceAmbiguous("Gmail evidence lacks exact SENT identity")
        milliseconds = int(internal_date)
        if milliseconds <= 0:
            raise ProviderEvidenceAmbiguous("Gmail provider execution time is invalid")
        try:
            executed_at = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        except (ValueError, OverflowError) as error:
            raise ProviderEvidenceAmbiguous("Gmail provider execution time is invalid") from error
        try:
            raw = _decode_provider_raw(evidence.get("raw"))
            actual_sender, args = _parse_exact_raw(raw, expected_message_id=message_id)
        except SendValidationError as error:
            raise ProviderEvidenceAmbiguous("Gmail raw evidence is not exact") from error
        if (
            not hmac.compare_digest(actual_sender, sender_address)
            or not hmac.compare_digest(args["to"], recipient)
            or not hmac.compare_digest(canonical_args_hash(args), payload_hash)
            or not hmac.compare_digest(sender_address, self._account_email)
        ):
            raise ProviderEvidenceAmbiguous("Gmail evidence does not match the approved account and payload")
        normalized = {
            "id": provider_id,
            "threadId": thread_id,
            "messageId": message_id,
            "connectionId": self._connection_id,
            "accountEmail": self._account_email,
            "senderAddress": actual_sender,
            "recipient": args["to"],
            "payloadHash": canonical_args_hash(args),
            "executedAt": executed_at.isoformat(),
            "labels": labels,
        }
        try:
            EffectReceipt.from_provider_evidence(normalized)
        except ValueError as error:
            raise ProviderEvidenceAmbiguous("Gmail evidence schema is invalid") from error
        return normalized

    def send_raw(
        self,
        *,
        raw: bytes,
        message_id: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> dict[str, object]:
        message_id = self._message_id(message_id)
        if not isinstance(idempotency_key, str) or not hmac.compare_digest(idempotency_key, message_id):
            raise SendValidationError("Gmail idempotency identity is not exact")
        sender, args = _parse_exact_raw(raw, expected_message_id=message_id)
        if (
            sender != self._account_email
            or canonical_args_hash(args) != payload_hash
        ):
            raise SendValidationError("raw Gmail message is not bound to the approved account and payload")
        self._assert_bound_provider_account()
        encoded = base64.urlsafe_b64encode(raw).decode("ascii")
        try:
            response = (
                self._messages()
                .send(userId=self._user_id, body={"raw": encoded})
                .execute()
            )
        except Exception as error:
            raise ProviderEvidenceAmbiguous("Gmail send outcome is unproven") from error
        if not isinstance(response, Mapping) or not isinstance(response.get("id"), str) or not response["id"]:
            raise ProviderEvidenceAmbiguous("Gmail send returned no provider identity")
        evidence = self._fetch_exact_evidence(
            provider_id=response["id"],
            message_id=message_id,
            sender_address=sender,
            recipient=args["to"],
            payload_hash=payload_hash,
        )
        if response.get("threadId") is not None and response.get("threadId") != evidence["threadId"]:
            raise ProviderEvidenceAmbiguous("Gmail send and evidence thread identities disagree")
        return evidence

    def find_by_message_id(
        self,
        *,
        message_id: str,
        sender_address: str,
        recipient: str,
        payload_hash: str,
    ) -> dict[str, object] | None:
        message_id = self._message_id(message_id)
        self._assert_bound_provider_account()
        bare_id = message_id[1:-1]
        try:
            response = (
                self._messages()
                .list(
                    userId=self._user_id,
                    q=f"rfc822msgid:{bare_id}",
                    maxResults=2,
                    includeSpamTrash=True,
                )
                .execute()
            )
        except Exception as error:
            raise ProviderEvidenceAmbiguous("Gmail history lookup is unavailable") from error
        if not isinstance(response, Mapping) or not isinstance(response.get("messages", []), list):
            raise ProviderEvidenceAmbiguous("Gmail history response is invalid")
        candidates = response.get("messages", [])
        if not candidates:
            return None
        if len(candidates) != 1 or not isinstance(candidates[0], Mapping):
            raise ProviderEvidenceAmbiguous("deterministic Message-ID resolved to multiple Gmail messages")
        provider_id = candidates[0].get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ProviderEvidenceAmbiguous("Gmail history has no message identity")
        return self._fetch_exact_evidence(
            provider_id=provider_id,
            message_id=message_id,
            sender_address=sender_address,
            recipient=recipient,
            payload_hash=payload_hash,
        )


def _aware_datetime(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError as error:
        raise SendValidationError(f"{label} is invalid") from error
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise SendValidationError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def _receipt_matches_action(receipt: EffectReceipt, action: Mapping[str, object]) -> bool:
    return (
        receipt.message_id == action.get("messageId")
        and receipt.connection_id == action.get("connectionId")
        and receipt.account_email == action.get("accountEmail")
        and receipt.sender_address == action.get("senderAddress")
        and receipt.recipient == action.get("args", {}).get("to")
        and receipt.payload_hash == action.get("payloadHash")
    )


class GmailSendExecutor:
    def __init__(
        self,
        *,
        state_machine,
        provider,
        founder_user_ids: Iterable[str],
        connection_id: str,
        account_email: str,
        sender_address: str,
        now=None,
    ) -> None:
        self._resource = gmail_resource(
            connection_id=connection_id, account_email=account_email
        )
        if not isinstance(sender_address, str) or _EMAIL.fullmatch(sender_address) is None:
            raise ValueError("sender_address must be one canonical email address")
        if sender_address != account_email:
            raise ValueError("v0 sender must equal the bound Google account")
        self._machine = state_machine
        self._provider = provider
        self._founders = frozenset(founder_user_ids)
        self._connection_id = connection_id
        self._account_email = account_email
        self._sender = sender_address
        self._now = now or (lambda: datetime.now(timezone.utc))

    def execute(self, action: Mapping[str, object]) -> EffectReceipt:
        if not isinstance(action, Mapping):
            raise SendValidationError("action must be an object")
        try:
            action_id = action["actionId"]
            user_id = action["userId"]
            if not isinstance(action_id, str) or not isinstance(user_id, str):
                raise TypeError("action identity must be text")
            persisted = self._machine.get(action_id=action_id, user_id=user_id)
            if not isinstance(persisted, Mapping):
                raise SendValidationError("action does not exist")
            action = persisted
            state = ActionState(action["state"])
            revision = action["revision"]
            draft_revision = action["draftRevision"]
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or isinstance(draft_revision, bool)
                or not isinstance(draft_revision, int)
                or draft_revision < 1
            ):
                raise TypeError("action revisions must be positive integers")
            args = validate_email_args(action["args"])
            payload_hash = action["payloadHash"]
            approval_hash = action["approvalArgsHash"]
            approved_hash = action["approvedArgsHash"]
            capability = action["capability"]
            resource = action["resource"]
            approval_id = action["approvalId"]
            approval_expires_at = _aware_datetime(action["approvalExpiresAt"], "approvalExpiresAt")
            approved_at = _aware_datetime(action["approvedAt"], "approvedAt")
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, SendValidationError):
                raise
            raise SendValidationError("action record is invalid") from error
        if user_id not in self._founders:
            raise CapabilityDenied("Gmail effects are founder-only")
        computed_hash = canonical_args_hash(args)
        if (
            approval_hash != computed_hash
            or payload_hash != computed_hash
            or approved_hash != computed_hash
            or action.get("approvalActionId") != action_id
            or action.get("approvedActionId") != action_id
            or action.get("approvalDraftRevision") != draft_revision
            or action.get("approvedDraftRevision") != draft_revision
            or capability != "gmail.send"
            or resource != self._resource
            or action.get("connectionId") != self._connection_id
            or action.get("accountEmail") != self._account_email
            or action.get("senderAddress") != self._sender
            or not isinstance(approval_id, str)
            or approved_at >= approval_expires_at
        ):
            raise SendValidationError("action does not match the approved exact payload and Google account")
        if state is ActionState.CONFIRMED:
            try:
                receipt = EffectReceipt.from_record(action.get("effectReceipt"))
            except ValueError as error:
                raise SendValidationError("confirmed action receipt is malformed") from error
            if not _receipt_matches_action(receipt, action):
                raise SendValidationError("receipt does not match the approved action")
            return receipt
        if state in {ActionState.DISPATCHING, ActionState.UNCERTAIN}:
            raise EffectUncertain("effect outcome requires provider reconciliation")
        if state is not ActionState.APPROVED:
            raise SendValidationError("action is not approved for dispatch")

        now = self._now().astimezone(timezone.utc)
        if now >= approval_expires_at:
            try:
                self._machine.transition(
                    action_id=action_id,
                    user_id=user_id,
                    current=ActionState.APPROVED,
                    target=ActionState.EXPIRED,
                    revision=revision,
                    updates={"expiredAt": now.isoformat()},
                )
            except Exception as error:
                raise EffectUncertain("approval expiry transition could not be proven") from error
            raise CapabilityDenied("approved Gmail effect expired before dispatch")

        message_id = deterministic_message_id(
            action_id=action_id,
            draft_revision=draft_revision,
            resource=resource,
            payload_hash=payload_hash,
        )
        message = EmailMessage(policy=SMTP)
        message["From"] = self._sender
        message["To"] = args["to"]
        message["Subject"] = args["subject"]
        message["Message-ID"] = message_id
        message.set_content(args["body"], subtype="plain", charset="utf-8")
        raw = message.as_bytes(policy=SMTP)

        dispatch_operation_id = self._machine.new_operation_id()
        try:
            dispatching = self._machine.transition(
                action_id=action_id,
                user_id=user_id,
                current=ActionState.APPROVED,
                target=ActionState.DISPATCHING,
                revision=revision,
                operation_id=dispatch_operation_id,
                updates={
                    "messageId": message_id,
                    "dispatchOperationId": dispatch_operation_id,
                    "dispatchDraftRevision": draft_revision,
                },
            )
        except Exception as error:
            raise EffectUncertain("another caller owns or may own dispatch") from error
        if (
            dispatching.get("dispatchOperationId") != dispatch_operation_id
            or dispatching.get("lastTransitionId") != dispatch_operation_id
        ):
            raise EffectUncertain("dispatch operation ownership is unproven")
        dispatch_revision = int(dispatching["revision"])
        try:
            outcome = self._provider.send_raw(
                raw=raw,
                message_id=message_id,
                idempotency_key=message_id,
                payload_hash=payload_hash,
            )
            receipt = EffectReceipt.from_provider_evidence(outcome)
            dispatched_action = {**action, **dispatching}
            if not _receipt_matches_action(receipt, dispatched_action):
                raise ProviderEvidenceAmbiguous("provider evidence is not the approved effect")
        except Exception as error:
            try:
                self._machine.transition(
                    action_id=action_id,
                    user_id=user_id,
                    current=ActionState.DISPATCHING,
                    target=ActionState.UNCERTAIN,
                    revision=dispatch_revision,
                    updates={
                        "uncertainAt": self._now().astimezone(timezone.utc).isoformat(),
                        "uncertaintyReason": "provider-outcome-unproven",
                        "uncertainDraftRevision": draft_revision,
                    },
                )
            except Exception:
                pass
            raise EffectUncertain("Gmail effect outcome is uncertain and was not retried") from error

        tracker = WaitingForReply(
            action_id=action_id,
            draft_revision=draft_revision,
            connection_id=self._connection_id,
            account_email=self._account_email,
            recipient=args["to"],
            message_id=message_id,
            provider_thread_id=receipt.provider_thread_id,
            since=receipt.executed_at,
        )
        try:
            self._machine.transition(
                action_id=action_id,
                user_id=user_id,
                current=ActionState.DISPATCHING,
                target=ActionState.CONFIRMED,
                revision=dispatch_revision,
                updates={
                    "effectReceipt": receipt.record(),
                    "waitingForReply": tracker.record(),
                    "confirmationMethod": "provider-send-evidence",
                    "confirmedAt": self._now().astimezone(timezone.utc).isoformat(),
                },
            )
        except Exception as error:
            try:
                self._machine.transition(
                    action_id=action_id,
                    user_id=user_id,
                    current=ActionState.DISPATCHING,
                    target=ActionState.UNCERTAIN,
                    revision=dispatch_revision,
                    updates={
                        "uncertainAt": self._now().astimezone(timezone.utc).isoformat(),
                        "uncertaintyReason": "confirmation-persistence-unproven",
                        "uncertainDraftRevision": draft_revision,
                    },
                )
            except Exception:
                pass
            raise EffectUncertain("Gmail evidence exists but durable confirmation is uncertain") from error
        return receipt
