import importlib.util
import json
from pathlib import Path
import sys


ROUTER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROUTER_DIR))
spec = importlib.util.spec_from_file_location(
    "telegram_ingress_under_test", ROUTER_DIR / "telegram_ingress.py"
)
ingress_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ingress_module
assert spec.loader is not None
spec.loader.exec_module(ingress_module)


SECRET = "webhook-secret-value"


class Resolver:
    def __init__(self, result=("user_a1", False)):
        self.result = result
        self.calls = []

    def __call__(self, channel, actor, display_name):
        self.calls.append((channel, actor, display_name))
        return self.result


class Queue:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"MessageId": "sqs-1", "SequenceNumber": "1"}


def update(*, update_id=100, text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 55,
            "chat": {"id": 9001},
            "from": {"id": 42, "first_name": "Ada"},
            "text": text,
        },
    }


def router(*, resolver=None, queue=None):
    return ingress_module.TelegramWebhookIngress(
        secret_provider=lambda: SECRET,
        resolve_user=resolver or Resolver(),
        sqs_client=queue or Queue(),
        queue_url="https://sqs.eu-west-1.amazonaws.com/1/personal-operator.fifo",
    )


def test_missing_or_wrong_secret_rejects_before_parse_resolve_or_enqueue():
    resolver = Resolver()
    queue = Queue()
    ingress = router(resolver=resolver, queue=queue)
    for headers in [{}, {"x-telegram-bot-api-secret-token": "wrong"}]:
        result = ingress.handle("{not-json", headers)
        assert result["statusCode"] == 401
    assert resolver.calls == []
    assert queue.calls == []


def test_valid_text_is_enqueued_once_before_immediate_success_ack():
    resolver = Resolver()
    queue = Queue()
    ingress = router(resolver=resolver, queue=queue)

    result = ingress.handle(
        json.dumps(update()),
        {"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert result == {"statusCode": 200, "body": "ok"}
    assert resolver.calls == [("telegram", "42", "Ada")]
    assert len(queue.calls) == 1
    request = queue.calls[0]
    assert request["MessageGroupId"] == "user_a1"
    wire = json.loads(request["MessageBody"])
    assert wire["kind"] == "message"
    assert wire["payload"] == {
        "chatId": "9001",
        "actorId": "telegram:42",
        "message": "hello",
    }
    assert request["MessageDeduplicationId"] == wire["traceId"]


def test_known_product_command_is_classified_locally_for_worker():
    queue = Queue()
    ingress = router(queue=queue)

    result = ingress.handle(
        json.dumps(update(text="/STATUS@MyBot")),
        {"x-telegram-bot-api-secret-token": SECRET},
    )

    assert result["statusCode"] == 200
    wire = json.loads(queue.calls[0]["MessageBody"])
    assert wire["kind"] == "command"
    assert wire["payload"]["command"] == "/status"


def test_one_hundred_replays_keep_one_bound_fifo_identity():
    queue = Queue()
    ingress = router(queue=queue)
    body = json.dumps(update())
    headers = {"x-telegram-bot-api-secret-token": SECRET}

    for _ in range(100):
        assert ingress.handle(body, headers)["statusCode"] == 200

    assert len({call["MessageDeduplicationId"] for call in queue.calls}) == 1
    assert len({call["MessageGroupId"] for call in queue.calls}) == 1


def test_enqueue_failure_returns_retryable_non_ack_and_never_runs_fallback():
    queue = Queue(error=TimeoutError("SQS outcome unknown"))
    ingress = router(queue=queue)

    result = ingress.handle(
        json.dumps(update()),
        {"x-telegram-bot-api-secret-token": SECRET},
    )

    assert result == {"statusCode": 503, "body": "queue unavailable"}
    assert len(queue.calls) == 1


def test_uninvited_invalid_and_non_text_updates_are_safe_noops_or_rejections():
    queue = Queue()
    uninvited = router(resolver=Resolver((None, False)), queue=queue)
    assert uninvited.handle(
        json.dumps(update()),
        {"x-telegram-bot-api-secret-token": SECRET},
    ) == {"statusCode": 200, "body": "ok"}
    assert queue.calls == []

    ingress = router(queue=queue)
    assert ingress.handle(
        json.dumps({"update_id": 101, "message": {"photo": [{"file_id": "x"}]}}),
        {"x-telegram-bot-api-secret-token": SECRET},
    ) == {"statusCode": 200, "body": "ok"}
    assert ingress.handle(
        "{bad-json",
        {"x-telegram-bot-api-secret-token": SECRET},
    )["statusCode"] == 400
    assert queue.calls == []
