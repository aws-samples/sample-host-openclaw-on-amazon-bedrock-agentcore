import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest


WORKER_DIR = Path(__file__).resolve().parent
TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef"
spec = importlib.util.spec_from_file_location(
    "worker_telegram_delivery", WORKER_DIR / "telegram_delivery.py"
)
delivery_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = delivery_module
assert spec.loader is not None
spec.loader.exec_module(delivery_module)


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, limit):
        assert limit == delivery_module.MAX_RESPONSE_BYTES + 1
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_one_exact_telegram_attempt_returns_provider_receipt_without_exposing_token():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response({"ok": True, "result": {"message_id": 12345}})

    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: TOKEN,
        opener=opener,
        timeout_seconds=10,
    )

    receipt = adapter.send_message(
        chat_id="9001",
        html="<b>Done</b>",
        trace_id="po1_" + "a" * 64,
        idempotency_key="po1_" + "a" * 64,
    )

    assert receipt == {"providerMessageId": "12345"}
    assert len(requests) == 1
    request, timeout = requests[0]
    assert timeout == 10
    assert request.method == "POST"
    assert request.headers["Content-type"] == "application/json"
    assert json.loads(request.data) == {
        "chat_id": "9001",
        "text": "<b>Done</b>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    assert "idempotency" not in request.data.decode().lower()
    assert TOKEN not in repr(receipt)


def test_callback_query_acknowledgement_is_one_fixed_content_free_attempt():
    requests = []
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: TOKEN,
        opener=lambda request, timeout: (
            requests.append((request, timeout))
            or Response({"ok": True, "result": True})
        ),
        timeout_seconds=7,
    )

    adapter.acknowledge_callback(
        callback_query_id="telegram_callback_query_123"
    )

    assert len(requests) == 1
    request, timeout = requests[0]
    assert timeout == 7
    assert request.full_url.endswith("/answerCallbackQuery")
    assert json.loads(request.data) == {
        "callback_query_id": "telegram_callback_query_123",
        "text": "Working...",
        "show_alert": False,
        "cache_time": 0,
    }


def test_callback_acknowledgement_validates_before_token_and_never_retries():
    token_calls = []
    attempts = []
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: token_calls.append(True) or TOKEN,
        opener=lambda *args, **kwargs: (
            attempts.append((args, kwargs))
            and None
        ),
    )
    for invalid in ("", "contains space", "x" * 257):
        with pytest.raises(delivery_module.TelegramDeliveryValidationError):
            adapter.acknowledge_callback(callback_query_id=invalid)
    assert token_calls == []
    assert attempts == []

    failing = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: TOKEN,
        opener=lambda *args, **kwargs: (
            attempts.append((args, kwargs))
            or (_ for _ in ()).throw(TimeoutError("lost"))
        ),
    )
    with pytest.raises(delivery_module.TelegramDeliveryUncertain):
        failing.acknowledge_callback(
            callback_query_id="telegram_callback_query_123"
        )
    assert len(attempts) == 1


def test_validated_opportunity_keyboard_is_included_in_the_same_provider_attempt():
    requests = []
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: TOKEN,
        opener=lambda request, timeout: (
            requests.append((request, timeout))
            or Response({"ok": True, "result": {"message_id": 12345}})
        ),
    )
    reply_markup = {
        "inline_keyboard": [[
            {"text": "Edit", "callback_data": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"},
            {"text": "Prepare", "callback_data": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"},
            {"text": "Skip", "callback_data": "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"},
            {"text": "Why", "callback_data": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"},
        ]]
    }

    adapter.send_message(
        chat_id="9001",
        html="<b>Reply to Ada</b>",
        reply_markup=reply_markup,
        trace_id="po1_" + "a" * 64,
        idempotency_key="po1_" + "a" * 64,
    )

    assert len(requests) == 1
    assert json.loads(requests[0][0].data)["reply_markup"] == reply_markup


def test_keyboard_validation_rejects_urls_send_and_action_mismatch_before_token_read():
    token_calls = []
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: token_calls.append(True),
    )
    for markup in (
        {"inline_keyboard": [[{"text": "Open", "url": "https://attacker.test"}]]},
        {"inline_keyboard": [[
            {"text": "Edit", "callback_data": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"},
            {"text": "Send", "callback_data": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"},
            {"text": "Skip", "callback_data": "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"},
            {"text": "Why", "callback_data": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"},
        ]]},
    ):
        with pytest.raises(delivery_module.TelegramDeliveryValidationError):
            adapter.send_message(
                chat_id="9001",
                html="safe",
                reply_markup=markup,
                trace_id="po1_" + "a" * 64,
                idempotency_key="po1_" + "a" * 64,
            )
    assert token_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("after send unknown"),
        OSError("connection reset"),
    ],
)
def test_network_failure_is_uncertain_and_never_retried_inside_adapter(failure):
    attempts = []

    def opener(*args, **kwargs):
        attempts.append((args, kwargs))
        raise failure

    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: TOKEN,
        opener=opener,
    )

    with pytest.raises(delivery_module.TelegramDeliveryUncertain):
        adapter.send_message(
            chat_id="9001",
            html="hello",
            trace_id="po1_" + "a" * 64,
            idempotency_key="po1_" + "a" * 64,
        )
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "description": "bad"},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": None}},
    ],
)
def test_missing_exact_provider_receipt_is_uncertain(payload):
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: TOKEN,
        opener=lambda *_args, **_kwargs: Response(payload),
    )
    with pytest.raises(delivery_module.TelegramDeliveryUncertain):
        adapter.send_message(
            chat_id="9001",
            html="hello",
            trace_id="po1_" + "a" * 64,
            idempotency_key="po1_" + "a" * 64,
        )


def test_local_validation_fails_before_token_or_network():
    token_calls = []
    network_calls = []
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: token_calls.append(True),
        opener=lambda *_args, **_kwargs: network_calls.append(True),
    )
    for kwargs in [
        {"chat_id": "not-id", "html": "hello"},
        {"chat_id": "1", "html": "x" * 4_097},
        {"chat_id": "1", "html": ""},
    ]:
        with pytest.raises(delivery_module.TelegramDeliveryValidationError):
            adapter.send_message(
                **kwargs,
                trace_id="po1_" + "a" * 64,
                idempotency_key="po1_" + "a" * 64,
            )
    assert token_calls == []
    assert network_calls == []


def test_shared_renderer_enforces_bound_after_html_escape_and_formatting():
    assert delivery_module.render_safe_telegram_html("<done> & **safe**") == (
        "&lt;done&gt; &amp; <b>safe</b>"
    )

    with pytest.raises(delivery_module.TelegramDeliveryValidationError):
        delivery_module.render_safe_telegram_html(
            "&" * (delivery_module.TELEGRAM_MAX_HTML_CHARS // 5 + 1)
        )


def test_adapter_reuses_exact_shared_html_bound_before_provider_attempt():
    token_calls = []
    network_calls = []
    adapter = delivery_module.TelegramDeliveryAdapter(
        token_provider=lambda: token_calls.append(True),
        opener=lambda *_args, **_kwargs: network_calls.append(True),
    )
    oversized = "x" * (delivery_module.TELEGRAM_MAX_HTML_CHARS + 1)

    with pytest.raises(delivery_module.TelegramDeliveryValidationError):
        adapter.send_message(
            chat_id="9001",
            html=oversized,
            trace_id="po1_" + "a" * 64,
            idempotency_key="po1_" + "a" * 64,
        )

    assert token_calls == []
    assert network_calls == []


def test_shared_bound_counts_utf16_units_conservatively_for_telegram():
    assert delivery_module.validate_safe_telegram_html("😀" * 2_048)
    with pytest.raises(delivery_module.TelegramDeliveryValidationError):
        delivery_module.validate_safe_telegram_html("😀" * 2_049)
