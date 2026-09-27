"""Unit tests for scripts/guardrail-eval.py (operator tool around ApplyGuardrail).

Zero AWS calls: the bedrock-runtime client is a MagicMock that returns fabricated
ApplyGuardrail responses. Runs with plain unittest or pytest:

    python3 -m unittest tests.test_guardrail_eval -v
    pytest tests/test_guardrail_eval.py -v
"""

import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "guardrail-eval.py"
_FIXTURES = _ROOT / "tests" / "fixtures" / "guardrail_prompts.json"
_CRON_LAMBDA = _ROOT / "lambda" / "cron" / "index.py"

_spec = importlib.util.spec_from_file_location("guardrail_eval", _SCRIPT)
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

CARD = "4111 1111 1111 1111"


# --- fabricated ApplyGuardrail responses ---------------------------------------


def _none():
    return {"action": "NONE", "outputs": [], "assessments": [{}]}


def _prompt_attack(confidence="HIGH", strength="HIGH", action="BLOCKED"):
    return {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Sorry, I can't help with that."}],
        "assessments": [
            {
                "contentPolicy": {
                    "filters": [
                        {
                            "type": "PROMPT_ATTACK",
                            "confidence": confidence,
                            "filterStrength": strength,
                            "action": action,
                            "detected": True,
                        }
                    ]
                }
            }
        ],
    }


def _card_blocked():
    return {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Sorry, I can't help with that."}],
        "assessments": [
            {
                "sensitiveInformationPolicy": {
                    "piiEntities": [
                        {"match": CARD, "type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCKED", "detected": True}
                    ]
                }
            }
        ],
    }


def _client_error(code="AccessDeniedException", message="not authorized", status=403):
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "ApplyGuardrail",
    )


def _write_fixtures(prompts):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"prompts": prompts}, tmp)
    tmp.close()
    return tmp.name


def _run(argv, client):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ge.main(argv, client=client)
    return code, out.getvalue(), err.getvalue()


BASE_ARGS = ["--guardrail-id", "gr-placeholder", "--version", "3"]


# --- classify(): parse assessments -----------------------------------------------


class ClassifyTest(unittest.TestCase):
    def test_no_intervention_is_allow(self):
        verdict, fired = ge.classify(_none())
        self.assertEqual(verdict, "allow")
        self.assertEqual(fired, [])

    def test_prompt_attack_reports_confidence_and_strength(self):
        verdict, fired = ge.classify(_prompt_attack(confidence="LOW", strength="HIGH"))
        self.assertEqual(verdict, "block")
        self.assertEqual(fired, ["contentPolicy PROMPT_ATTACK (confidence LOW, strength HIGH) BLOCKED"])

    def test_pii_card_block_does_not_leak_match(self):
        verdict, fired = ge.classify(_card_blocked())
        self.assertEqual(verdict, "block")
        self.assertEqual(fired, ["sensitiveInformation CREDIT_DEBIT_CARD_NUMBER BLOCKED"])
        self.assertNotIn("4111", " ".join(fired))

    def test_topic_regex_and_word_policies(self):
        resp = {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [
                {"topicPolicy": {"topics": [{"name": "CryptoScams", "type": "DENY", "action": "BLOCKED"}]}},
                {
                    "sensitiveInformationPolicy": {
                        "regexes": [{"name": "AWSAccessKeyId", "match": "AKIA...", "regex": "x", "action": "BLOCKED"}]
                    },
                    "wordPolicy": {
                        "customWords": [{"match": "AKIA", "action": "BLOCKED"}],
                        "managedWordLists": [{"match": "darn", "type": "PROFANITY", "action": "BLOCKED"}],
                    },
                },
            ],
        }
        verdict, fired = ge.classify(resp)
        self.assertEqual(verdict, "block")
        self.assertEqual(
            fired,
            [
                "topicPolicy CryptoScams BLOCKED",
                "sensitiveInformation regex AWSAccessKeyId BLOCKED",
                "wordPolicy custom word BLOCKED",
                "wordPolicy PROFANITY BLOCKED",
            ],
        )

    def test_anonymize_only_is_allow_but_reported(self):
        resp = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "mail {EMAIL}"}],
            "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": [{"match": "x", "type": "EMAIL", "action": "ANONYMIZED"}]}}
            ],
        }
        verdict, fired = ge.classify(resp)
        self.assertEqual(verdict, "allow")
        self.assertEqual(fired, ["sensitiveInformation EMAIL ANONYMIZED"])

    def test_detect_mode_hit_is_allow_but_reported(self):
        verdict, fired = ge.classify(_prompt_attack(action="NONE"))
        self.assertEqual(verdict, "allow")
        self.assertEqual(fired, ["contentPolicy PROMPT_ATTACK (confidence HIGH, strength HIGH) NONE (detected)"])

    def test_intervened_without_readable_assessment_is_block(self):
        verdict, fired = ge.classify({"action": "GUARDRAIL_INTERVENED", "assessments": []})
        self.assertEqual(verdict, "block")
        self.assertEqual(fired, ["(intervened, no policy detail)"])


# --- fixtures ------------------------------------------------------------------


class FixturesTest(unittest.TestCase):
    def test_cron_prefix_matches_cron_lambda(self):
        # The eval must send exactly what lambda/cron/index.py sends, or the
        # "does the prefix itself contribute?" measurement is meaningless.
        src = _CRON_LAMBDA.read_text()
        self.assertIn('cron_message = f"[Scheduled task: {schedule_name or schedule_id}] {message}"', src)
        self.assertEqual(ge.cron_message("Morning brief", "hello"), "[Scheduled task: Morning brief] hello")

    def test_schedule_name_expands_to_plain_and_cron_variant(self):
        path = _write_fixtures(
            [
                {"id": "brief", "expected": "allow", "schedule_name": "Daily brief", "text": "Summarise the news."},
                {"id": "card", "expected": "block", "text": f"My card is {CARD}"},
            ]
        )
        cases = ge.load_cases(path)
        self.assertEqual([c["id"] for c in cases], ["brief", "brief+cron", "card"])
        self.assertEqual(cases[1]["text"], "[Scheduled task: Daily brief] Summarise the news.")
        self.assertEqual(cases[1]["expected"], "allow")

    def test_invalid_expected_rejected(self):
        path = _write_fixtures([{"id": "x", "expected": "maybe", "text": "hi"}])
        with self.assertRaises(ValueError):
            ge.load_cases(path)

    def test_duplicate_id_rejected(self):
        path = _write_fixtures(
            [{"id": "x", "expected": "allow", "text": "a"}, {"id": "x", "expected": "allow", "text": "b"}]
        )
        with self.assertRaises(ValueError):
            ge.load_cases(path)

    def test_shipped_fixture_file_is_valid_and_synthetic(self):
        cases = ge.load_cases(str(_FIXTURES))
        expected = {c["expected"] for c in cases}
        self.assertEqual(expected, {"allow", "block"})
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))
        # every brief-style prompt is also exercised with the cron prefix
        self.assertTrue(any(i.endswith("+cron") for i in ids))
        raw = _FIXTURES.read_text()
        self.assertIn("4111 1111 1111 1111", raw)  # the synthetic test card, nothing real
        self.assertIsNone(re.search(r"\b\d{12}\b", raw), "no account-id-like numbers")
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw), "no email addresses")
        self.assertNotIn("arn:aws", raw)


# --- main(): exit codes, request shape, output -----------------------------------


class MainTest(unittest.TestCase):
    def setUp(self):
        self.path = _write_fixtures(
            [
                {"id": "brief", "expected": "allow", "text": "Summarise today's tech news."},
                {"id": "card", "expected": "block", "text": f"Charge {CARD} please"},
            ]
        )

    def _client(self, side_effect):
        client = MagicMock()
        client.apply_guardrail = MagicMock(side_effect=side_effect)
        return client

    def test_all_match_exits_zero_and_sends_input_source(self):
        client = self._client([_none(), _card_blocked()])
        code, out, _ = _run(BASE_ARGS + ["--fixtures", self.path], client)
        self.assertEqual(code, 0)
        first = client.apply_guardrail.call_args_list[0].kwargs
        self.assertEqual(
            first,
            {
                "guardrailIdentifier": "gr-placeholder",
                "guardrailVersion": "3",
                "source": "INPUT",
                "content": [{"text": {"text": "Summarise today's tech news."}}],
            },
        )
        self.assertIn("brief", out)
        self.assertIn("sensitiveInformation CREDIT_DEBIT_CARD_NUMBER BLOCKED", out)
        self.assertIn("2/2 matched", out)
        self.assertNotIn("4111", out)  # never echo prompt text or PII matches

    def test_mismatch_exits_one(self):
        client = self._client([_prompt_attack(), _card_blocked()])
        code, out, _ = _run(BASE_ARGS + ["--fixtures", self.path], client)
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)
        self.assertIn("contentPolicy PROMPT_ATTACK (confidence HIGH, strength HIGH) BLOCKED", out)
        self.assertIn("1/2 matched", out)

    def test_api_error_marks_row_continues_and_exits_two(self):
        client = self._client([_client_error(), _card_blocked()])
        code, out, err = _run(BASE_ARGS + ["--fixtures", self.path], client)
        self.assertEqual(code, 2)
        self.assertEqual(client.apply_guardrail.call_count, 2)
        self.assertIn("ERROR", out)
        self.assertIn("AccessDeniedException", out + err)

    def test_api_error_takes_precedence_over_mismatch(self):
        client = self._client([_client_error(), _none()])
        code, _, _ = _run(BASE_ARGS + ["--fixtures", self.path], client)
        self.assertEqual(code, 2)

    def test_missing_fixture_file_exits_two_without_calling_api(self):
        client = self._client([])
        code, _, err = _run(BASE_ARGS + ["--fixtures", "/nonexistent/prompts.json"], client)
        self.assertEqual(code, 2)
        client.apply_guardrail.assert_not_called()
        self.assertIn("fixtures", err.lower())

    def test_region_passed_to_boto3_client(self):
        stub = self._client([_none(), _card_blocked()])
        with patch.object(ge.boto3, "client", return_value=stub) as factory:
            out = io.StringIO()
            with redirect_stdout(out):
                code = ge.main(BASE_ARGS + ["--fixtures", self.path, "--region", "us-west-2"])
        self.assertEqual(code, 0)
        factory.assert_called_once_with("bedrock-runtime", region_name="us-west-2")


if __name__ == "__main__":
    unittest.main()
