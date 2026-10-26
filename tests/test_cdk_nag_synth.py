"""The whole app synthesizes with no unsuppressed cdk-nag errors.

app.py applies AwsSolutionsChecks to every stack; an error annotation makes
`cdk deploy` refuse the stack. A new wildcard IAM statement therefore needs a
matching, justified `applies_to` entry in the stack's AwsSolutions-IAM5
suppression. No AWS calls.

    pytest tests/test_cdk_nag_synth.py -v
"""

import pytest

from tests._app_synth import synth_errors


@pytest.mark.parametrize("context", [
    {},
    {"environment_suffix": "stg"},
    {"enable_gateway": True},
], ids=["default", "environment-suffix", "gateway"])
def test_no_unsuppressed_nag_errors(context):
    assert synth_errors(**context) == []
