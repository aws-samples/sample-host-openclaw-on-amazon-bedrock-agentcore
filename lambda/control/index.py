"""Pure trusted product-command application, separate from the Linux runtime."""

from __future__ import annotations

import re
from typing import Callable, Mapping
from urllib.parse import quote

from .telegram_cards import CardActionAlreadyUsed, CardActionRejected


_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_TRACE_ID = re.compile(r"po1_[0-9a-f]{64}")
_CHAT_ID = re.compile(r"-?[0-9]{1,20}")
_ACTOR_ID = re.compile(r"telegram:[0-9]{1,20}")
_CALLBACK_DATA = re.compile(
    r"poc1:(edit|prepare|skip|why):[A-Za-z0-9_-]{22,32}"
)
_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_APPROVAL_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,2048}")
_COMMANDS = frozenset(
    {"/start", "/connect", "/scan", "/tasks", "/workspace", "/status", "/delete"}
)
_PRODUCT_FIELDS = {
    "action",
    "userId",
    "channel",
    "command",
    "chatId",
    "actorId",
    "traceId",
    "idempotencyKey",
}
_LEGACY_PRODUCT_FIELDS = {
    "action",
    "userId",
    "channel",
    "command",
    "traceId",
    "idempotencyKey",
}
_CALLBACK_FIELDS = {
    "action",
    "userId",
    "channel",
    "chatId",
    "actorId",
    "callbackData",
    "traceId",
    "idempotencyKey",
}
_DELETION_FENCE_FIELDS = {
    "action",
    "userId",
    "channel",
    "traceId",
    "idempotencyKey",
}


class ControlRequestError(ValueError):
    pass


class ControlApplication:
    def __init__(
        self,
        *,
        tickets,
        gmail,
        tasks,
        deletion_intents,
        web_origin: str,
        approval_producer=None,
        card_actions=None,
        draft_preparer=None,
        scan_measurements=None,
    ) -> None:
        if not isinstance(web_origin, str) or not web_origin.startswith("https://"):
            raise ValueError("web origin must use HTTPS")
        self._tickets = tickets
        self._gmail = gmail
        self._tasks = tasks
        if not callable(getattr(deletion_intents, "get_deletion_intent", None)):
            raise TypeError("deletion intent store is invalid")
        self._deletion_intents = deletion_intents
        self._web_origin = web_origin.rstrip("/")
        self._approval_producer = approval_producer
        if card_actions is not None and (
            not callable(getattr(card_actions, "issue", None))
            or not callable(getattr(card_actions, "consume", None))
        ):
            raise TypeError("card action store is invalid")
        if draft_preparer is not None and not callable(
            getattr(draft_preparer, "prepare", None)
        ):
            raise TypeError("draft preparer is invalid")
        self._card_actions = card_actions
        self._draft_preparer = draft_preparer
        if scan_measurements is not None and any(
            not callable(getattr(scan_measurements, method, None))
            for method in ("start", "complete", "fail")
        ):
            raise TypeError("scan measurement store is invalid")
        self._scan_measurements = scan_measurements

    def _ticket_url(self, *, user_id: str, return_path: str) -> str:
        ticket = self._tickets.issue(user_id=user_id, return_path=return_path)
        return f"{self._web_origin}/?ticket={quote(ticket, safe='')}"

    @staticmethod
    def _binding(request: Mapping) -> tuple[str, str]:
        user_id = request.get("userId")
        trace_id = request.get("traceId")
        if (
            request.get("channel") != "telegram"
            or not isinstance(user_id, str)
            or _USER_ID.fullmatch(user_id) is None
            or not isinstance(trace_id, str)
            or _TRACE_ID.fullmatch(trace_id) is None
            or request.get("idempotencyKey") != trace_id
        ):
            raise ControlRequestError("control request binding is invalid")
        return user_id, trace_id

    @staticmethod
    def _validate_product(
        request: object,
    ) -> tuple[str, str, str, str | None, str | None]:
        if not isinstance(request, Mapping) or frozenset(request) not in {
            frozenset(_PRODUCT_FIELDS),
            frozenset(_LEGACY_PRODUCT_FIELDS),
        }:
            raise ControlRequestError("control request fields are invalid")
        user_id, trace_id = ControlApplication._binding(request)
        command = request.get("command")
        if (
            request.get("action") != "productCommand"
            or command not in _COMMANDS
        ):
            raise ControlRequestError("control request binding is invalid")
        chat_id = request.get("chatId")
        actor_id = request.get("actorId")
        if set(request) == _PRODUCT_FIELDS and (
            not isinstance(chat_id, str)
            or _CHAT_ID.fullmatch(chat_id) is None
            or not isinstance(actor_id, str)
            or _ACTOR_ID.fullmatch(actor_id) is None
        ):
            raise ControlRequestError("Telegram delivery binding is invalid")
        return user_id, trace_id, command, chat_id, actor_id

    @staticmethod
    def _validate_callback(request: object) -> tuple[str, str, str, str, str]:
        if not isinstance(request, Mapping) or set(request) != _CALLBACK_FIELDS:
            raise ControlRequestError("callback request fields are invalid")
        user_id, trace_id = ControlApplication._binding(request)
        chat_id = request.get("chatId")
        actor_id = request.get("actorId")
        callback_data = request.get("callbackData")
        if (
            request.get("action") != "telegramCallback"
            or not isinstance(chat_id, str)
            or _CHAT_ID.fullmatch(chat_id) is None
            or not isinstance(actor_id, str)
            or _ACTOR_ID.fullmatch(actor_id) is None
            or not isinstance(callback_data, str)
            or _CALLBACK_DATA.fullmatch(callback_data) is None
        ):
            raise ControlRequestError("callback request binding is invalid")
        return user_id, trace_id, chat_id, actor_id, callback_data

    @staticmethod
    def _validate_deletion_fence(request: object) -> tuple[str, str]:
        if not isinstance(request, Mapping) or set(request) != _DELETION_FENCE_FIELDS:
            raise ControlRequestError("deletion fence request fields are invalid")
        user_id, trace_id = ControlApplication._binding(request)
        if request.get("action") != "deletionFence":
            raise ControlRequestError("deletion fence request binding is invalid")
        return user_id, trace_id

    def _scan(
        self,
        user_id: str,
        *,
        chat_id: str | None,
        actor_id: str | None,
    ) -> tuple[str, dict[str, object] | None]:
        scan_id = (
            self._scan_measurements.start(user_id)
            if self._scan_measurements is not None
            else None
        )
        try:
            opportunities = self._gmail.scan(user_id=user_id)
            if not isinstance(opportunities, list) or len(opportunities) > 3:
                raise ControlRequestError("workflow returned invalid opportunities")
        except Exception as error:
            if self._scan_measurements is not None:
                self._scan_measurements.fail(
                    user_id,
                    scan_id,
                    failure_code=self._scan_failure_code(error),
                )
            raise
        if self._scan_measurements is not None:
            self._scan_measurements.complete(
                user_id,
                scan_id,
                result_count=len(opportunities),
            )
        if not opportunities:
            return "No unanswered follow-ups were found in the 3–30 day window.", None
        lines = ["I found these unanswered follow-ups:"]
        visible = opportunities[:3]
        for index, item in enumerate(visible, 1):
            # Keep three cards inside Telegram's post-escape UTF-16 limit even
            # when every untrusted character expands to an HTML entity. The
            # single-card Why action retains the full bounded reason.
            title = str(getattr(item, "title", ""))[:80]
            reason = str(getattr(item, "reason", ""))[:120]
            link = str(getattr(getattr(item, "source", None), "deep_link", ""))[:512]
            if not title or not reason or not link.startswith("https://"):
                raise ControlRequestError("workflow returned an invalid opportunity")
            lines.extend(["", f"{index}. {title}", reason, link])
        telegram = None
        if (
            self._card_actions is not None
            and chat_id is not None
            and actor_id is not None
        ):
            issued = self._card_actions.issue(
                user_id=user_id,
                chat_id=chat_id,
                actor_id=actor_id,
                opportunities=visible,
            )
            cards = []
            for card in issued:
                render = getattr(card, "to_control", None)
                if not callable(render):
                    raise ControlRequestError("card store returned invalid data")
                cards.append(render())
            if len(cards) != len(visible):
                raise ControlRequestError("card store returned the wrong result count")
            telegram = {
                "inlineKeyboard": [card["buttons"] for card in cards]
            }
            lines.extend(["", "Choose an action below. No button sends email."])
        else:
            lines.extend(
                [
                    "",
                    "Edit, prepare, skip, or ask why in your private control surface:",
                    self._ticket_url(user_id=user_id, return_path="/workspace"),
                ]
            )
        return "\n".join(lines), telegram

    @staticmethod
    def _scan_failure_code(error: BaseException) -> str:
        name = type(error).__name__.casefold()
        if isinstance(error, PermissionError) or any(
            marker in name
            for marker in ("oauth", "credential", "authorization", "tokenenvelope")
        ):
            return "AUTHORIZATION"
        if "ranker" in name:
            return "RANKING"
        if isinstance(error, (TimeoutError, ConnectionError, OSError)) or (
            "provider" in name
        ):
            return "PROVIDER_UNAVAILABLE"
        return "INTERNAL"

    @staticmethod
    def _validated_prepared(
        value: object,
        *,
        label: str,
        require_local_revision: bool = False,
    ) -> tuple[str, str | None]:
        action_id = getattr(value, "action_id", None)
        token = getattr(value, "token", None)
        revision = getattr(value, "revision", None)
        if (
            not isinstance(action_id, str)
            or _ACTION_ID.fullmatch(action_id) is None
            or (require_local_revision and (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision != 1
            ))
            or (token is not None and (
                not isinstance(token, str)
                or _APPROVAL_TOKEN.fullmatch(token) is None
            ))
        ):
            raise ControlRequestError(f"{label} returned invalid data")
        return action_id, token

    def _callback(
        self,
        *,
        user_id: str,
        chat_id: str,
        actor_id: str,
        callback_data: str,
    ) -> str:
        if self._card_actions is None:
            raise ControlRequestError("Telegram card actions are unavailable")
        try:
            consumed = self._card_actions.consume(
                user_id=user_id,
                chat_id=chat_id,
                actor_id=actor_id,
                callback_data=callback_data,
            )
        except CardActionAlreadyUsed:
            return "This button was already used. Nothing was sent. Run /scan to refresh."
        except CardActionRejected as error:
            raise ControlRequestError("Telegram card action is unavailable") from error
        action = getattr(consumed, "action", None)
        opportunity = getattr(consumed, "opportunity", None)
        title = str(getattr(opportunity, "title", ""))[:120]
        reason = str(getattr(opportunity, "reason", ""))[:280]
        source = str(getattr(getattr(opportunity, "source", None), "deep_link", ""))[:512]
        opportunity_id = str(getattr(opportunity, "id", ""))[:128]
        if (
            action not in {"edit", "prepare", "skip", "why"}
            or not title
            or not reason
            or not source.startswith("https://")
            or not opportunity_id
        ):
            raise ControlRequestError("stored Telegram card action is invalid")
        if action == "skip":
            return f"Skipped {title} for this scan. Nothing was sent."
        if action == "why":
            return f"Why this appeared: {reason}\n{source}\n\nNothing was sent."

        # Both Edit and Prepare first create the same deterministic local
        # revision. This gives the browser a real persisted object to edit;
        # the preparer has no provider client or send capability.
        if self._draft_preparer is None:
            raise ControlRequestError("read-only draft preparation is unavailable")
        prepared_draft = self._draft_preparer.prepare(
            user_id=user_id,
            opportunity=opportunity,
        )
        draft_action_id, _ = self._validated_prepared(
            prepared_draft,
            label="draft preparer",
            require_local_revision=True,
        )
        draft_url = self._ticket_url(
            user_id=user_id,
            return_path=f"/workspace?draft={draft_action_id}",
        )
        if action == "edit":
            return (
                f"Edit the private draft for {title}:\n"
                f"{draft_url}\n\n"
                "Nothing was sent."
            )
        lines = [
            f"Prepared a private draft for {title} ({draft_action_id}).",
            "Nothing was sent.",
            f"Review it at {draft_url}",
        ]
        if self._approval_producer is not None:
            prepare = getattr(self._approval_producer, "prepare", None)
            if not callable(prepare):
                raise TypeError("approval producer is invalid")
            approval = prepare(user_id=user_id, opportunity=opportunity)
            if approval is not None:
                _, token = self._validated_prepared(
                    approval,
                    label="approval producer",
                )
                if token is None:
                    raise ControlRequestError("approval producer returned no token")
                lines.extend(
                    [
                        "",
                        "Review the exact governed draft before it can be sent:",
                        f"{self._web_origin}/approve/{quote(token, safe='')}",
                    ]
                )
        return "\n".join(lines)

    def _tasks_text(self, user_id: str) -> str:
        tasks = self._tasks.list_open(user_id)
        if not tasks:
            return "No governed tasks are waiting right now."
        lines = ["Open governed tasks:"]
        for task in tasks[:10]:
            if not isinstance(task, Mapping):
                raise ControlRequestError("task source returned invalid data")
            lines.append(f"• {str(task.get('title', 'Task'))[:120]} — {str(task.get('state', ''))[:40]}")
        lines.append(
            "Review them at "
            + self._ticket_url(user_id=user_id, return_path="/workspace")
        )
        return "\n".join(lines)

    def handle(self, request: object) -> dict[str, object]:
        if isinstance(request, Mapping) and request.get("action") == "deletionFence":
            user_id, trace_id = self._validate_deletion_fence(request)
            blocked = self._deletion_intents.get_deletion_intent(user_id) is not None
            return {
                "status": "ok",
                "userId": user_id,
                "traceId": trace_id,
                "blocked": blocked,
            }
        if isinstance(request, Mapping) and request.get("action") == "telegramCallback":
            user_id, trace_id, chat_id, actor_id, callback_data = (
                self._validate_callback(request)
            )
            command = None
        else:
            user_id, trace_id, command, chat_id, actor_id = (
                self._validate_product(request)
            )
            callback_data = None
        # This is deliberately the first product lookup. A strong-consistent
        # deletion fence blocks tickets, provider reads, drafts, and records,
        # including after the byte purge has reached COMPLETED.
        if self._deletion_intents.get_deletion_intent(user_id) is not None:
            raise ControlRequestError("account deletion has already been requested")
        telegram = None
        if callback_data is not None:
            text = self._callback(
                user_id=user_id,
                chat_id=chat_id,
                actor_id=actor_id,
                callback_data=callback_data,
            )
        elif command == "/connect":
            text = (
                "Open your private control surface:\n"
                f"{self._ticket_url(user_id=user_id, return_path='/connections')}\n\n"
                "This link is one-time and expires in five minutes."
            )
        elif command == "/scan":
            text, telegram = self._scan(
                user_id,
                chat_id=chat_id,
                actor_id=actor_id,
            )
        elif command == "/tasks":
            text = self._tasks_text(user_id)
        elif command == "/workspace":
            text = (
                "Open your portable workspace:\n"
                f"{self._ticket_url(user_id=user_id, return_path='/workspace')}\n\n"
                "Export your portable workspace:\n"
                f"{self._ticket_url(user_id=user_id, return_path='/export')}"
            )
        elif command == "/status":
            text = (
                "The trusted command plane accepted this request. "
                "Open your private status overview:\n"
                f"{self._ticket_url(user_id=user_id, return_path='/')}"
            )
        elif command == "/delete":
            text = (
                "Open "
                f"{self._ticket_url(user_id=user_id, return_path='/delete')} "
                "to review deletion. "
                "This command itself does not delete anything."
            )
        else:
            text = (
                "Personal Operator is ready. Open your private connection setup:\n"
                f"{self._ticket_url(user_id=user_id, return_path='/connections')}\n\n"
                "Use /scan for follow-ups, or send a normal request to your workspace."
            )
        if not isinstance(text, str) or not 1 <= len(text) <= 3_500:
            raise ControlRequestError("control response exceeds its boundary")
        result = {
            "status": "ok",
            "userId": user_id,
            "traceId": trace_id,
            "text": text,
        }
        if telegram is not None:
            result["telegram"] = telegram
        return result


_application_factory: Callable[[], ControlApplication] | None = None
_production_application: ControlApplication | None = None


def configure_application_factory(factory: Callable[[], ControlApplication]) -> None:
    global _application_factory
    if not callable(factory):
        raise TypeError("control application factory must be callable")
    _application_factory = factory


def _application() -> ControlApplication:
    global _production_application
    if _application_factory is not None:
        application = _application_factory()
    else:
        if _production_application is None:
            from .composition import build_production_application

            _production_application = build_production_application()
        application = _production_application
    if not isinstance(application, ControlApplication):
        raise TypeError("control application factory returned an invalid value")
    return application


def lambda_handler(event, context):
    del context
    return _application().handle(event)
