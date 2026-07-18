from __future__ import annotations

import pytest

from .telegram_cards import (
    TelegramCardValidationError,
    TelegramCommandResult,
    decode_ledger_result,
    encode_ledger_result,
)


KEYBOARD = (
    (
        ("Edit", "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"),
        ("Prepare", "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"),
        ("Skip", "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"),
        ("Why", "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"),
    ),
)


def test_validated_card_result_round_trips_through_plain_text_ledger():
    result = TelegramCommandResult(
        text="**Reply to Ada**\nWaiting seven days.",
        inline_keyboard=KEYBOARD,
    )

    encoded = encode_ledger_result(result)
    decoded = decode_ledger_result(encoded)

    assert decoded == result
    assert isinstance(encoded, str)
    assert len(encoded) <= 3_500
    assert decoded.reply_markup() == {
        "inline_keyboard": [[
            {"text": "Edit", "callback_data": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"},
            {"text": "Prepare", "callback_data": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"},
            {"text": "Skip", "callback_data": "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"},
            {"text": "Why", "callback_data": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"},
        ]]
    }


def test_control_schema_is_exact_and_maps_only_callback_buttons():
    result = TelegramCommandResult.from_control(
        text="safe",
        telegram={
            "inlineKeyboard": [[
                {"text": "Edit", "callbackData": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"},
                {"text": "Prepare", "callbackData": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"},
                {"text": "Skip", "callbackData": "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"},
                {"text": "Why", "callbackData": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"},
            ]]
        },
    )

    assert result.inline_keyboard == KEYBOARD

    with pytest.raises(TelegramCardValidationError):
        TelegramCommandResult.from_control(
            text="safe",
            telegram={
                "inlineKeyboard": [[{
                    "text": "Open",
                    "url": "https://attacker.example",
                    "callbackData": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA",
                }]]
            },
        )


@pytest.mark.parametrize(
    "keyboard",
    [
        ((
            ("Edit", "poc1:why:AAAAAAAAAAAAAAAAAAAAAA"),
            ("Prepare", "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"),
            ("Skip", "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"),
            ("Why", "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"),
        ),),
        KEYBOARD * 4,
        ((
            ("Send", "poc1:prepare:AAAAAAAAAAAAAAAAAAAAAA"),
            ("Prepare", "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"),
            ("Skip", "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"),
            ("Why", "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"),
        ),),
    ],
)
def test_keyboard_rejects_action_mismatch_extra_rows_and_send_button(keyboard):
    with pytest.raises(TelegramCardValidationError):
        TelegramCommandResult(text="safe", inline_keyboard=keyboard)


def test_legacy_plain_command_result_remains_a_safe_buttonless_message():
    result = decode_ledger_result("already persisted")
    assert result == TelegramCommandResult(text="already persisted")
    assert result.reply_markup() is None


def test_ledger_encoding_does_not_expand_safe_unicode_into_false_oversize_json():
    result = TelegramCommandResult(
        text="😀" * 1_600,
        inline_keyboard=KEYBOARD,
    )

    assert decode_ledger_result(encode_ledger_result(result)) == result
