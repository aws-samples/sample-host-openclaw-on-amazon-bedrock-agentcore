"""Opaque, single-use Telegram card actions bound to derived Gmail evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
import secrets
from typing import Callable, Mapping, Sequence

from workflows.gmail.models import Opportunity, SourceEvidence
from workflows.index import GmailPilotWorkflow


CARD_TTL = timedelta(days=14)
CARD_ACTIONS = ("edit", "prepare", "skip", "why")
CARD_LABELS = ("Edit", "Prepare", "Skip", "Why")
_CALLBACK_DATA = re.compile(
    r"poc1:(edit|prepare|skip|why):([A-Za-z0-9_-]{22,32})"
)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22,32}")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_CHAT_ID = re.compile(r"-?[0-9]{1,20}")
_ACTOR_ID = re.compile(r"telegram:[0-9]{1,20}")


class CardActionStoreError(RuntimeError):
    pass


class CardActionRejected(CardActionStoreError):
    pass


class CardActionAlreadyUsed(CardActionRejected):
    pass


def _identity(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _clock(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CardActionStoreError("card clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    return None


def _callback_key(user_id: str, callback_data: str) -> dict[str, str]:
    digest = hashlib.sha256(callback_data.encode("ascii")).hexdigest()
    return {"PK": f"USER#{user_id}", "SK": f"TELEGRAM_CALLBACK#{digest}"}


def _snapshot(opportunity: Opportunity) -> dict[str, object]:
    if not isinstance(opportunity, Opportunity):
        raise TypeError("card requires a validated Opportunity")
    source = opportunity.source
    return {
        "id": opportunity.id,
        "userId": opportunity.user_id,
        "waitingSince": opportunity.waiting_since.isoformat(),
        "title": opportunity.title,
        "reason": opportunity.reason,
        "confidence": str(opportunity.confidence),
        "source": {
            "sourceId": source.source_id,
            "threadId": source.thread_id,
            "deepLink": source.deep_link,
            "correspondent": source.correspondent,
            "subject": source.subject,
            "excerpt": source.excerpt,
        },
    }


def _opportunity(value: object, *, user_id: str) -> Opportunity:
    if not isinstance(value, Mapping) or set(value) != {
        "id",
        "userId",
        "waitingSince",
        "title",
        "reason",
        "confidence",
        "source",
    }:
        raise CardActionStoreError("stored card opportunity is invalid")
    if value.get("userId") != user_id:
        raise CardActionStoreError("stored card belongs to another user")
    source = value.get("source")
    if not isinstance(source, Mapping) or set(source) != {
        "sourceId",
        "threadId",
        "deepLink",
        "correspondent",
        "subject",
        "excerpt",
    }:
        raise CardActionStoreError("stored card source is invalid")
    try:
        waiting = datetime.fromisoformat(value["waitingSince"])
        evidence = SourceEvidence(
            source_id=source["sourceId"],
            thread_id=source["threadId"],
            deep_link=source["deepLink"],
            correspondent=source["correspondent"],
            subject=source["subject"],
            excerpt=source["excerpt"],
            waiting_since=waiting,
        )
        return Opportunity(
            id=value["id"],
            user_id=user_id,
            source=evidence,
            waiting_since=waiting,
            title=value["title"],
            reason=value["reason"],
            confidence=float(value["confidence"]),
        )
    except (TypeError, ValueError, OverflowError):
        raise CardActionStoreError("stored card opportunity is invalid") from None


@dataclass(frozen=True, slots=True)
class TelegramOpportunityCard:
    opportunity: Opportunity
    buttons: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity, Opportunity):
            raise TypeError("card opportunity is invalid")
        if len(self.buttons) != len(CARD_ACTIONS):
            raise ValueError("card must have four actions")
        for index, button in enumerate(self.buttons):
            if (
                not isinstance(button, tuple)
                or len(button) != 2
                or button[0] != CARD_LABELS[index]
                or not isinstance(button[1], str)
            ):
                raise ValueError("card button is invalid")
            match = _CALLBACK_DATA.fullmatch(button[1])
            if match is None or match.group(1) != CARD_ACTIONS[index]:
                raise ValueError("card callback binding is invalid")

    def to_control(self) -> dict[str, object]:
        return {
            "title": self.opportunity.title,
            "reason": self.opportunity.reason,
            "sourceUrl": self.opportunity.source.deep_link,
            "buttons": [
                {"text": label, "callbackData": callback_data}
                for label, callback_data in self.buttons
            ],
        }


@dataclass(frozen=True, slots=True)
class ConsumedCardAction:
    action: str
    opportunity: Opportunity

    def __post_init__(self) -> None:
        if self.action not in CARD_ACTIONS or not isinstance(
            self.opportunity, Opportunity
        ):
            raise ValueError("consumed card action is invalid")


class DynamoTelegramCardActions:
    """Issue random handles and atomically consume them exactly once."""

    def __init__(
        self,
        table,
        *,
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        conditional_failure_types: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._table = table
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._tokens = token_factory or (lambda: secrets.token_urlsafe(18))
        self._conditional_failures = conditional_failure_types

    def _read(self, user_id: str, callback_data: str) -> Mapping[str, object] | None:
        response = self._table.get_item(
            Key=_callback_key(user_id, callback_data),
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None

    def issue(
        self,
        *,
        user_id: str,
        chat_id: str,
        actor_id: str,
        opportunities: Sequence[Opportunity],
    ) -> list[TelegramOpportunityCard]:
        user_id = _identity(user_id, _USER_ID, "user identity")
        chat_id = _identity(chat_id, _CHAT_ID, "chat identity")
        actor_id = _identity(actor_id, _ACTOR_ID, "actor identity")
        if (
            not isinstance(opportunities, Sequence)
            or isinstance(opportunities, (str, bytes))
            or len(opportunities) > 3
        ):
            raise ValueError("at most three opportunities may become cards")
        now = _clock(self._now())
        created_at = int(now.timestamp())
        expiry = int((now + CARD_TTL).timestamp())
        cards: list[TelegramOpportunityCard] = []
        for opportunity in opportunities:
            snapshot = _snapshot(opportunity)
            if opportunity.user_id != user_id:
                raise ValueError("card opportunity belongs to another user")
            buttons: list[tuple[str, str]] = []
            for label, action in zip(CARD_LABELS, CARD_ACTIONS, strict=True):
                token = self._tokens()
                if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
                    raise CardActionStoreError("card token factory returned invalid data")
                callback_data = f"poc1:{action}:{token}"
                item = {
                    **_callback_key(user_id, callback_data),
                    "recordType": "TELEGRAM_CARD_ACTION",
                    "userId": user_id,
                    "chatId": chat_id,
                    "actorId": actor_id,
                    "action": action,
                    "opportunity": snapshot,
                    "createdAt": created_at,
                    "ttl": expiry,
                }
                try:
                    self._table.put_item(
                        Item=item,
                        ConditionExpression=(
                            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                        ),
                    )
                except Exception as error:
                    stored = self._read(user_id, callback_data)
                    if not isinstance(stored, Mapping) or dict(stored) != item:
                        if isinstance(error, self._conditional_failures):
                            raise CardActionStoreError("card token collided") from None
                        raise CardActionStoreError("card issue is uncertain") from None
                buttons.append((label, callback_data))
            cards.append(TelegramOpportunityCard(opportunity, tuple(buttons)))
        return cards

    @staticmethod
    def _matches(
        item: Mapping[str, object],
        *,
        user_id: str,
        chat_id: str,
        actor_id: str,
        action: str,
        now_epoch: int,
    ) -> bool:
        ttl = _integer(item.get("ttl"))
        return bool(
            item.get("recordType") == "TELEGRAM_CARD_ACTION"
            and item.get("userId") == user_id
            and item.get("chatId") == chat_id
            and item.get("actorId") == actor_id
            and item.get("action") == action
            and ttl is not None
            and ttl > now_epoch
        )

    def consume(
        self,
        *,
        user_id: str,
        chat_id: str,
        actor_id: str,
        callback_data: str,
    ) -> ConsumedCardAction:
        user_id = _identity(user_id, _USER_ID, "user identity")
        chat_id = _identity(chat_id, _CHAT_ID, "chat identity")
        actor_id = _identity(actor_id, _ACTOR_ID, "actor identity")
        if not isinstance(callback_data, str):
            raise CardActionRejected("card action is invalid")
        match = _CALLBACK_DATA.fullmatch(callback_data)
        if match is None:
            raise CardActionRejected("card action is invalid")
        action = match.group(1)
        now_epoch = int(_clock(self._now()).timestamp())
        try:
            response = self._table.update_item(
                Key=_callback_key(user_id, callback_data),
                UpdateExpression="SET consumedAt=:now",
                ConditionExpression=(
                    "recordType=:recordType AND userId=:userId AND "
                    "chatId=:chatId AND actorId=:actorId AND #action=:action AND "
                    "#ttl>:now AND attribute_not_exists(consumedAt)"
                ),
                ExpressionAttributeNames={"#action": "action", "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":recordType": "TELEGRAM_CARD_ACTION",
                    ":userId": user_id,
                    ":chatId": chat_id,
                    ":actorId": actor_id,
                    ":action": action,
                    ":now": now_epoch,
                },
                ReturnValues="ALL_NEW",
            )
            item = response.get("Attributes") if isinstance(response, Mapping) else None
            if not isinstance(item, Mapping):
                raise CardActionStoreError("card consume returned no exact record")
        except Exception:
            item = self._read(user_id, callback_data)
            if (
                isinstance(item, Mapping)
                and self._matches(
                    item,
                    user_id=user_id,
                    chat_id=chat_id,
                    actor_id=actor_id,
                    action=action,
                    now_epoch=now_epoch,
                )
                and _integer(item.get("consumedAt")) is not None
            ):
                # Includes an ambiguous successful write. Do not return work
                # because that could duplicate the downstream draft mutation.
                raise CardActionAlreadyUsed("card action was already used") from None
            raise CardActionRejected("card action is unavailable") from None
        if not self._matches(
            item,
            user_id=user_id,
            chat_id=chat_id,
            actor_id=actor_id,
            action=action,
            now_epoch=now_epoch,
        ) or _integer(item.get("consumedAt")) != now_epoch:
            raise CardActionStoreError("consumed card record is invalid")
        return ConsumedCardAction(
            action=action,
            opportunity=_opportunity(item.get("opportunity"), user_id=user_id),
        )


class ReadOnlyGmailDraftPreparer:
    """Persist a deterministic local draft; it has no Gmail effect capability."""

    def __init__(self, repository, *, now: Callable[[], datetime] | None = None) -> None:
        self._repository = repository
        self._now = now or (lambda: datetime.now(timezone.utc))

    def prepare(self, *, user_id: str, opportunity: Opportunity):
        user_id = _identity(user_id, _USER_ID, "user identity")
        if not isinstance(opportunity, Opportunity) or opportunity.user_id != user_id:
            raise TypeError("draft requires a user-bound Opportunity")
        identity = hashlib.sha256(
            (
                "personal-operator-readonly-draft-v1\0"
                + user_id
                + "\0"
                + opportunity.id
                + "\0"
                + opportunity.source.source_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        topic = opportunity.source.subject or opportunity.title
        subject = ("Re: " + topic)[:200]
        body = (
            "Hello,\n\n"
            f"Following up on my previous email about {topic[:120]}.\n\n"
            "Best,"
        )
        return GmailPilotWorkflow(
            scanner=None,
            ranker=None,
            repository=self._repository,
            now=self._now,
        ).prepare_draft(
            user_id=user_id,
            action_id=f"draft_{identity}",
            revision=1,
            to=opportunity.source.correspondent,
            subject=subject,
            body=body,
        )
