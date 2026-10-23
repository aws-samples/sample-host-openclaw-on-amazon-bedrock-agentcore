"""Router timing: an async self-invocation that fails is not re-run by Lambda."""

from tests.test_runtime_routing_synth import run_app


def test_router_lambda_never_async_retries_a_turn(tmp_path):
    template = run_app(tmp_path)["OpenClawRouter.template.json"]
    resources = template["Resources"]
    function_id = next(
        key for key, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "openclaw-router"
    )
    configs = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::Lambda::EventInvokeConfig"]
    assert len(configs) == 1, "a failed self-invocation must not re-run the agent turn"
    assert configs[0]["MaximumRetryAttempts"] == 0
    assert configs[0]["FunctionName"] == {"Ref": function_id}
    # No dead-letter queue or destination: failures stay visible in the Errors metric and logs.
    assert "DestinationConfig" not in configs[0]
    assert "DeadLetterConfig" not in resources[function_id]["Properties"]
