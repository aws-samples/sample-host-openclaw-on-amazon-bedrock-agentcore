import importlib.util
from pathlib import Path
import sys

import pytest


GMAIL_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gmail_models_exact", GMAIL_DIR / "models.py")
models = importlib.util.module_from_spec(spec)
sys.modules["gmail_models_exact"] = models
assert spec.loader is not None
spec.loader.exec_module(models)


def test_draft_normalizes_address_and_subject_but_preserves_exact_body_bytes():
    body = "  First line\r\nSecond line  \n"

    draft = models.DraftRevision.create(
        action_id="action_12345678",
        revision=1,
        to="  person@example.net  ",
        subject="  Following up  ",
        body=body,
    )

    assert draft.to == "person@example.net"
    assert draft.subject == "Following up"
    assert draft.body == body
    assert draft.payload_hash == models.DraftRevision.compute_payload_hash(
        to="person@example.net",
        subject="Following up",
        body=body,
    )


@pytest.mark.parametrize("body", ["", "\x00hidden", "\ud800"])
def test_draft_rejects_empty_nul_or_non_utf8_body_without_rewriting(body):
    with pytest.raises((TypeError, ValueError, UnicodeError)):
        models.DraftRevision.create(
            action_id="action_12345678",
            revision=1,
            to="person@example.net",
            subject="Following up",
            body=body,
        )
