"""Strict Telegram opportunity-card result carried through the text ledger."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Mapping


MAX_LEDGER_RESULT_CHARS = 3_500
MAX_CARD_ROWS = 3
CARD_ACTIONS = ("edit", "prepare", "skip", "why")
CARD_LABELS = ("Edit", "Prepare", "Skip", "Why")
CALLBACK_DATA = re.compile(
    r"poc1:(edit|prepare|skip|why):([A-Za-z0-9_-]{22,32})"
)
_SCHEMA = "personal-operator.telegram-result.v1"


class TelegramCardValidationError(ValueError):
    pass


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_LEDGER_RESULT_CHARS:
        raise TelegramCardValidationError("Telegram result text is invalid")
    return value


def _keyboard(value: object) -> tuple[tuple[tuple[str, str], ...], ...]:
    if value in (None, (), []):
        return ()
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_CARD_ROWS:
        raise TelegramCardValidationError("Telegram card rows are invalid")
    result: list[tuple[tuple[str, str], ...]] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != len(CARD_ACTIONS):
            raise TelegramCardValidationError("Telegram card must have four actions")
        validated: list[tuple[str, str]] = []
        for index, button in enumerate(row):
            if (
                not isinstance(button, (list, tuple))
                or len(button) != 2
                or button[0] != CARD_LABELS[index]
                or not isinstance(button[1], str)
            ):
                raise TelegramCardValidationError("Telegram card button is invalid")
            match = CALLBACK_DATA.fullmatch(button[1])
            if match is None or match.group(1) != CARD_ACTIONS[index]:
                raise TelegramCardValidationError("Telegram callback action is invalid")
            if button[1] in seen:
                raise TelegramCardValidationError("Telegram callback action is duplicated")
            seen.add(button[1])
            validated.append((button[0], button[1]))
        result.append(tuple(validated))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TelegramCommandResult:
    text: str
    inline_keyboard: tuple[tuple[tuple[str, str], ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _text(self.text))
        object.__setattr__(self, "inline_keyboard", _keyboard(self.inline_keyboard))

    @classmethod
    def from_control(
        cls,
        *,
        text: object,
        telegram: object,
    ) -> "TelegramCommandResult":
        if not isinstance(telegram, Mapping) or set(telegram) != {"inlineKeyboard"}:
            raise TelegramCardValidationError("control Telegram result is invalid")
        raw_rows = telegram["inlineKeyboard"]
        if not isinstance(raw_rows, list):
            raise TelegramCardValidationError("control Telegram rows are invalid")
        rows: list[list[tuple[str, str]]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, list):
                raise TelegramCardValidationError("control Telegram row is invalid")
            row: list[tuple[str, str]] = []
            for raw_button in raw_row:
                if not isinstance(raw_button, Mapping) or set(raw_button) != {
                    "text",
                    "callbackData",
                }:
                    raise TelegramCardValidationError("control Telegram button is invalid")
                row.append((raw_button["text"], raw_button["callbackData"]))
            rows.append(row)
        return cls(text=_text(text), inline_keyboard=_keyboard(rows))

    def reply_markup(self) -> dict[str, list[list[dict[str, str]]]] | None:
        if not self.inline_keyboard:
            return None
        return {
            "inline_keyboard": [
                [
                    {"text": label, "callback_data": callback_data}
                    for label, callback_data in row
                ]
                for row in self.inline_keyboard
            ]
        }

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "text": self.text,
            "inlineKeyboard": [
                [
                    {"text": label, "callbackData": callback_data}
                    for label, callback_data in row
                ]
                for row in self.inline_keyboard
            ],
        }


def encode_ledger_result(value: str | TelegramCommandResult) -> str:
    result = value if isinstance(value, TelegramCommandResult) else TelegramCommandResult(value)
    encoded = json.dumps(
        result.to_wire(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) > MAX_LEDGER_RESULT_CHARS:
        raise TelegramCardValidationError("encoded Telegram result exceeds ledger bound")
    return encoded


def decode_ledger_result(value: object) -> TelegramCommandResult:
    value = _text(value)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return TelegramCommandResult(value)
    if not isinstance(decoded, Mapping) or decoded.get("schema") != _SCHEMA:
        return TelegramCommandResult(value)
    if set(decoded) != {"schema", "text", "inlineKeyboard"}:
        raise TelegramCardValidationError("stored Telegram result has invalid fields")
    return TelegramCommandResult.from_control(
        text=decoded["text"],
        telegram={"inlineKeyboard": decoded["inlineKeyboard"]},
    )


def validate_reply_markup(value: object) -> dict[str, object] | None:
    """Validate provider-shaped markup without permitting URLs or effect buttons."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"inline_keyboard"}:
        raise TelegramCardValidationError("Telegram reply markup is invalid")
    rows = value["inline_keyboard"]
    if not isinstance(rows, list):
        raise TelegramCardValidationError("Telegram reply rows are invalid")
    normalized: list[list[tuple[str, str]]] = []
    for row in rows:
        if not isinstance(row, list):
            raise TelegramCardValidationError("Telegram reply row is invalid")
        normalized_row: list[tuple[str, str]] = []
        for button in row:
            if not isinstance(button, Mapping) or set(button) != {
                "text",
                "callback_data",
            }:
                raise TelegramCardValidationError("Telegram reply button is invalid")
            normalized_row.append((button["text"], button["callback_data"]))
        normalized.append(normalized_row)
    checked = _keyboard(normalized)
    return TelegramCommandResult(text="validated", inline_keyboard=checked).reply_markup()
