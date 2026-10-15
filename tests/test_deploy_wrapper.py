import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/deploy.sh", scripts)
    shutil.copy(ROOT / "scripts/build_and_push.sh", scripts)
    context = {
        "region": "us-west-2", "runtime_id": "active-1234567890",
        "runtime_endpoint_id": "DEFAULT", "enable_guardrails": True,
        "enable_gateway": True, "enable_browser": True, "image_version": "70",
    }
    (tmp_path / "cdk.json").write_text(json.dumps({"context": context}))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable = binaries / "fake"
    executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, re, sys
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
record = {"tool": tool, "args": args}
if tool == "deployment-python" and args[1:2] == ["deploy"]:
    record["configuration"] = json.loads(pathlib.Path(args[args.index("--config") + 1]).read_text())
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps(record) + "\n")
if tool == "uv":
    if os.environ.get("FAIL_UV"):
        sys.exit(41)
    # Record the wrapper's command and forward it; do not emulate uv resolution.
    os.execv(args[3], [args[3], *args[5:]])
if tool == "deployment-python" and args[0] == "-c":
    if os.environ.get("FAIL_NOTIFICATION_JSON") and '"chat_id"' in args[1]:
        sys.exit(45)
    os.execv(sys.executable, [sys.executable, *args])
if (tool == "deployment-python" and args[0].endswith("runtime_deploy.py")
        and os.environ.get("CONTROLLER_STATE")):
    sys.path.insert(0, str(pathlib.Path(os.environ["SOURCE_SCRIPTS"]).parent / "tests"))
    from test_runtime_deploy import FakeControl, FakeInvoke, deploy
    from types import SimpleNamespace
    from unittest.mock import patch
    import boto3
    from botocore.exceptions import ClientError
    state_path = pathlib.Path(os.environ["CONTROLLER_STATE"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    control, invoke = FakeControl(), FakeInvoke()
    for key in ("current", "versions", "endpoints", "endpoint"):
        if key in state:
            setattr(control, key, state[key])
    def existing_caller(**kwargs):
        existing = os.environ.get("EXISTING_CALLER")
        if not existing:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetFunctionConfiguration")
        arn = control.current["agentRuntimeArn"]
        if existing == "other":
            arn = arn.replace("openclaw_agent_v2", "previous_v2")
        variables = {"AGENTCORE_RUNTIME_ARN": arn}
        if existing != "legacy":
            variables["RUNTIME_TARGET"] = json.dumps({"platformVersion": "V2", "runtimeArn": arn})
        return {"Environment": {"Variables": variables}}
    clients = {
        "bedrock-agentcore-control": control, "bedrock-agentcore": invoke,
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "123456789012"}),
        "lambda": SimpleNamespace(get_function_configuration=existing_caller),
        "ecr": SimpleNamespace(describe_images=lambda **kwargs: {
            "imageDetails": [{"imageDigest": "sha256:" + "a" * 64}],
        }),
    }
    try:
        with patch.object(boto3, "client", side_effect=lambda name, **kwargs: clients[name]):
            deploy.main(args[1:])
    finally:
        saved = {key: getattr(control, key) for key in ("current", "versions", "endpoints", "endpoint")}
        saved["calls"] = state.get("calls", []) + control.calls
        saved["invokes"] = state.get("invokes", 0) + len(invoke.calls)
        state_path.write_text(json.dumps(saved))
    sys.exit(0)
if tool == "aws":
    if args[:2] == ["cloudformation", "describe-stacks"]:
        key = re.search("OutputKey=='([^']+)'", args[args.index("--query") + 1]).group(1)
        values = {"ExecutionRoleArn": "arn:aws:iam::123456789012:role/openclaw",
                  "SecurityGroupId": "sg-12345678", "PrivateSubnetIds": "subnet-12345678",
                  "UserFilesBucketName": "user-bucket", "CognitoUserPoolId": "pool",
                  "CognitoProxyClientId": "client", "SecretsCmkArn": "arn:aws:kms:us-west-2:123456789012:key/key",
                  "GuardrailId": "guardrail", "GuardrailVersion": "1", "GatewayUrl": "https://example.com/mcp",
                  "BrowserIdentifier": "browser-1234567890"}
        if key == "BrowserIdentifier":
            if os.environ.get("FAIL_BROWSER_LOOKUP"):
                print("AccessDenied: BrowserIdentifier stack lookup failed", file=sys.stderr)
                sys.exit(37)
            if "BROWSER_OUTPUT" in os.environ:
                values[key] = os.environ["BROWSER_OUTPUT"]
        print(values[key])
    elif args[:2] == ["secretsmanager", "get-secret-value"]:
        if os.environ.get("FAIL_NOTIFICATION_SECRET"):
            sys.exit(44)
        print("synthetic-token")
    else:
        print("{}")
elif tool == "deployment-python":
    if args[0].endswith("runtime_config.py"):
        sys.path.insert(0, os.environ["SOURCE_SCRIPTS"])
        from runtime_config import foundation_configuration
        print(json.dumps(foundation_configuration(os.environ)))
    elif args[0].endswith("runtime_image.py"):
        print("123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge")
    elif args[:2] == ["-m", "pytest"]:
        print(os.environ.get("FAKE_PYTEST_OUTPUT", ""), end="")
        sys.exit(int(os.environ.get("FAKE_PYTEST_EXIT", "0")))
    elif args[1] == "deploy":
        if os.environ.get("FAIL_DEPLOY"):
            sys.exit(43)
        manifest = pathlib.Path(args[args.index("--manifest") + 1])
        manifest.parent.mkdir(parents=True, exist_ok=True)
        endpoint = args[args.index("--endpoint-name") + 1] if "--endpoint-name" in args else "release_1"
        manifest.write_text(json.dumps({"agentRuntimeId": "openclaw_agent_v2-1234567890", "endpointName": endpoint,
                                       "platformVersion": "V2",
                                       "warmup": {"status": "skipped" if "--skip-warmup" in args else "succeeded"}}))
    elif args[1] == "verify":
        if os.environ.get("FAIL_VERIFY"):
            sys.exit(2)
        manifest = pathlib.Path(args[args.index("--manifest") + 1])
        if manifest.exists() and json.loads(manifest.read_text()).get("platformVersion") != "V2":
            sys.exit(2)
        if manifest.exists() and "--require-enrollment-ready" in args and json.loads(manifest.read_text()).get("warmup", {}).get("status") != "succeeded":
            sys.exit(2)
elif tool == "docker" and args[0] == "login":
    sys.stdin.read()
elif tool == "curl" and os.environ.get("FAIL_NOTIFICATION_CURL"):
    sys.exit(22)
elif tool == "tee":
    output = sys.stdin.read()
    pathlib.Path(args[0]).write_text(output)
    sys.stdout.write(output)
    sys.exit(int(os.environ.get("FAKE_TEE_EXIT", "0")))
''')
    executable.chmod(0o755)
    for name in ("aws", "cdk", "deployment-python", "docker", "curl", "uv"):
        (binaries / name).symlink_to(executable)
    venv = tmp_path / ".venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    (venv / "activate").write_text("# fake CDK environment\n")
    env = {
        **os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "CALL_LOG": str(tmp_path / "calls.jsonl"), "CDK_DEFAULT_ACCOUNT": "123456789012",
        "CDK_DEFAULT_REGION": "us-west-2", "DEPLOY_PYTHON": str(binaries / "deployment-python"),
        "SOURCE_SCRIPTS": str(ROOT / "scripts"),
        "E2E_TELEGRAM_CHAT_ID": "", "E2E_RESULTS_FILE": str(tmp_path / "e2e-results.txt"),
    }
    return tmp_path, env


@pytest.fixture
def project_without_uv(project):
    root, env = project
    binaries = root / "bin"
    (binaries / "uv").unlink()
    for name in ("bash", "dirname"):
        (binaries / name).symlink_to(shutil.which(name))
    env["PATH"] = str(binaries)
    env.pop("CDK_DEFAULT_ACCOUNT", None)
    env.pop("CDK_DEFAULT_REGION", None)
    return project


def calls(project):
    root, _ = project
    return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("mode", ["--runtime-only", None])
def test_wrapper_uses_one_interpreter_and_keeps_context(project, mode):
    root, env = project
    env.pop("CDK_DEFAULT_REGION", None)
    before = (root / "cdk.json").read_bytes()
    command = ["bash", str(root / "scripts/deploy.sh"),
               "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw:v1"]
    if mode:
        command.append(mode)
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / "cdk.json").read_bytes() == before
    recorded = calls(project)
    python_calls = [item["args"] for item in recorded if item["tool"] == "deployment-python"]
    uv_calls = [item["args"] for item in recorded if item["tool"] == "uv"]
    assert uv_calls
    assert all(args[:5] == ["run", "--no-project", "--python", env["DEPLOY_PYTHON"], "python"]
               for args in uv_calls)
    assert [args[5:] for args in uv_calls] == python_calls
    assert python_calls[0][0] == "-c"
    controller_calls = [args for args in python_calls if args[0].endswith("runtime_deploy.py")]
    assert controller_calls[0][1] == "preflight"
    deployed = next(args for args in python_calls if args[1:2] == ["deploy"])
    assert not {"--platform", "--active-runtime-id", "--source-runtime-id", "--runtime-id"}.intersection(deployed)
    assert not any(item["tool"] == "agentcore" for item in recorded)
    runtime_env = next(item["configuration"]["environmentVariables"] for item in recorded
                       if "configuration" in item)
    assert runtime_env["BEDROCK_MODEL_ID"] == "global.anthropic.claude-opus-5-5"
    assert runtime_env["BEDROCK_FALLBACK_MODEL_ID"] == "global.anthropic.claude-sonnet-5-5"
    assert runtime_env["SUBAGENT_BEDROCK_MODEL_ID"] == "global.anthropic.claude-sonnet-5-5"
    if mode is None:
        dependent = [item["args"] for item in recorded if item["tool"] == "cdk"][-1]
        assert "runtime_manifest=.runtime/runtime-v2.json" in dependent
        verified = next(args for args in python_calls if args[1:2] == ["verify"])
        assert "--require-enrollment-ready" in verified
        assert "--platform" not in verified


def test_wrapper_passes_explicit_model_configuration(project):
    root, env = project
    config = json.loads((root / "cdk.json").read_text())
    config["context"].update(
        default_model_id="custom-primary", fallback_model_id="custom-fallback",
        subagent_model_id="custom-subagent",
    )
    (root / "cdk.json").write_text(json.dumps(config))
    result = subprocess.run([
        "bash", str(root / "scripts/deploy.sh"), "--runtime-only",
        "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw:v1",
    ], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    runtime_env = next(item["configuration"]["environmentVariables"] for item in calls(project)
                       if "configuration" in item)
    assert runtime_env["BEDROCK_MODEL_ID"] == "custom-primary"
    assert runtime_env["BEDROCK_FALLBACK_MODEL_ID"] == "custom-fallback"
    assert runtime_env["SUBAGENT_BEDROCK_MODEL_ID"] == "custom-subagent"


def test_phase1_uses_cdk_python_without_deployment_venv(project):
    root, env = project
    env["DEPLOY_PYTHON"] = str(root / ".venv-deploy/bin/python")
    env.pop("CDK_DEFAULT_REGION", None)
    assert not Path(env["DEPLOY_PYTHON"]).exists()
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--phase1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = calls(project)
    uv_calls = [item["args"] for item in recorded if item["tool"] == "uv"]
    assert uv_calls
    assert all(args[:6] == ["run", "--no-project", "--python", str(root / ".venv/bin/python"),
                           "python", "-c"] for args in uv_calls)
    cdk_calls = [item["args"] for item in recorded if item["tool"] == "cdk"]
    assert len(cdk_calls) == 1
    assert "OpenClawGateway" in cdk_calls[0]
    assert not any(item["tool"] == "deployment-python" for item in recorded)


def test_runtime_only_needs_no_cdk_environment_and_preserves_arguments(project):
    root, env = project
    shutil.rmtree(root / ".venv")
    (root / "bin/cdk").unlink()
    interpreter = root / "custom interpreter/deployment-python"
    interpreter.parent.mkdir()
    interpreter.symlink_to(root / "bin/deployment-python")
    env["DEPLOY_PYTHON"] = str(interpreter)
    manifest = root / "runtime manifests/runtime-v2.json"
    image = "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw:v1"
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only",
                             "--image-uri", image, "--manifest", str(manifest),
                             "--endpoint-name", "release_2", "--skip-warmup"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = calls(project)
    uv_calls = [item["args"] for item in recorded if item["tool"] == "uv"]
    assert uv_calls
    assert all(args[:5] == ["run", "--no-project", "--python", str(interpreter), "python"]
               for args in uv_calls)
    deployed = next(item["args"] for item in recorded
                    if item["tool"] == "deployment-python" and item["args"][1:2] == ["deploy"])
    assert deployed[deployed.index("--manifest") + 1] == str(manifest)
    assert deployed[deployed.index("--image-uri") + 1] == image
    assert deployed[deployed.index("--endpoint-name") + 1] == "release_2"
    assert "--skip-warmup" in deployed
    assert manifest.is_file()
    assert not any(item["tool"] == "cdk" for item in recorded)


@pytest.mark.parametrize("mode", [[], ["--phase1"], ["--runtime-only"], ["--phase3"], ["--cdk-only"]])
def test_missing_uv_stops_before_aws_calls(project_without_uv, mode):
    root, env = project_without_uv
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), *mode],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "install uv" in result.stderr
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("arguments,returncode,message", [
    (["--help"], 0, "Usage:"),
    (["--unknown"], 1, "unknown option"),
    (["--platform", "V1"], 1, "unknown option --platform"),
    (["--platform"], 1, "unknown option --platform"),
    (["--new-runtime"], 1, "unknown option --new-runtime"),
    (["--enable-v2-new-registrations"], 1, "unknown option"),
    (["--disable-v2-new-registrations"], 1, "unknown option"),
    (["--enable-v1-fallback"], 1, "unknown option"),
    (["--disable-v1-fallback"], 1, "unknown option"),
])
def test_arguments_are_handled_without_uv(project_without_uv, arguments, returncode, message):
    root, env = project_without_uv
    env["RUNTIME_PLATFORM"] = "V2"
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), *arguments],
                            env=env, capture_output=True, text=True)
    assert result.returncode == returncode
    assert message in result.stdout + result.stderr
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("script", ["deploy.sh", "e2e-deploy-and-test.sh"])
@pytest.mark.parametrize("platform", ["V1", "V2", ""])
def test_retired_platform_environment_is_rejected_before_external_commands(project, script, platform):
    root, env = project
    if script == "e2e-deploy-and-test.sh":
        shutil.copy(ROOT / "scripts" / script, root / "scripts")
    env["RUNTIME_PLATFORM"] = platform
    result = subprocess.run(["bash", str(root / "scripts" / script)],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "RUNTIME_PLATFORM is no longer supported" in result.stderr
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("mode", ["--phase1", "--runtime-only"])
def test_missing_interpreter_stops_before_aws_calls(project, mode):
    root, env = project
    env.pop("CDK_DEFAULT_ACCOUNT", None)
    env.pop("CDK_DEFAULT_REGION", None)
    if mode == "--phase1":
        (root / ".venv/bin/python").unlink()
    else:
        env.pop("DEPLOY_PYTHON", None)
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), mode],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    if mode == "--phase1":
        assert "install requirements.txt in .venv" in result.stderr
    else:
        assert "uv venv .venv-deploy" in result.stderr
        assert "uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt" in result.stderr
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("read_region", [False, True])
def test_uv_error_propagates_before_aws_calls(project, read_region):
    root, env = project
    env["FAIL_UV"] = "1"
    if read_region:
        env.pop("CDK_DEFAULT_REGION", None)
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 41
    recorded = calls(project)
    assert len(recorded) == 1
    assert recorded[0]["tool"] == "uv"
    assert recorded[0]["args"][5] == ("-c" if read_region else str(root / "scripts/runtime_deploy.py"))


def test_phase1_uv_error_propagates_before_cdk_with_region_set(project):
    root, env = project
    env.update(CDK_DEFAULT_REGION="us-west-2", FAIL_UV="1")
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--phase1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 41
    recorded = calls(project)
    uv_calls = [item["args"] for item in recorded if item["tool"] == "uv"]
    assert len(uv_calls) == 1
    assert uv_calls[0][5] == "-c"
    assert "enable_gateway" in uv_calls[0][6]
    assert not any(item["tool"] == "cdk" for item in recorded)


def test_phase3_failed_readback_prevents_cdk_deploy(project):
    root, env = project
    env["FAIL_VERIFY"] = "1"
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--phase3"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert not any(item["tool"] == "cdk" for item in calls(project))
    assert any(item["args"][1:2] == ["verify"] for item in calls(project))
    verify = next(item["args"] for item in calls(project)
                  if item["tool"] == "deployment-python" and item["args"][1:2] == ["verify"])
    assert "--check-existing-callers" in verify


def test_missing_legacy_context_still_prepares_v2(project):
    root, env = project
    (root / "cdk.json").write_text(json.dumps({"context": {"runtime_id": "", "enable_guardrails": False}}))
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only",
                             "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw:v1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    deployed = next(item["args"] for item in calls(project)
                    if item["tool"] == "deployment-python" and item["args"][1:2] == ["deploy"])
    assert "--runtime-id" not in deployed
    assert "--active-runtime-id" not in deployed


def test_local_build_resolves_repository_before_arm64_push(project):
    root, env = project
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = calls(project)
    repository_call = next(i for i, item in enumerate(recorded)
                           if item["args"][0].endswith("runtime_image.py"))
    build_call = next(i for i, item in enumerate(recorded)
                      if item["tool"] == "docker" and item["args"][:2] == ["buildx", "build"])
    assert repository_call < build_call
    build = recorded[build_call]["args"]
    assert build[build.index("--platform") + 1] == "linux/arm64"
    assert "--push" in build


def test_existing_codebuild_image_skips_local_repository_and_docker(project):
    root, env = project
    env["BUILD_MODE"] = "codebuild"
    env["RUNTIME_IMAGE_URI"] = "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v70"
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = calls(project)
    assert not any(item["tool"] == "docker" for item in recorded)
    assert not any(item["args"][0].endswith("runtime_image.py") for item in recorded)


def test_e2e_deploy_delegates_to_v2_controller_and_stops_on_failure(project):
    root, env = project
    shutil.copy(ROOT / "scripts/e2e-deploy-and-test.sh", root / "scripts")
    env["FAIL_DEPLOY"] = "1"
    result = subprocess.run(["bash", str(root / "scripts/e2e-deploy-and-test.sh"),
                             "--test-node", "tests/e2e/selected_test.py::test_chat"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 43, result.stderr
    recorded = calls(project)
    deployed = next(item["args"] for item in recorded
                    if item["tool"] == "deployment-python" and item["args"][1:2] == ["deploy"])
    assert "--platform" not in deployed
    assert "--skip-warmup" not in deployed
    assert not any("pytest" in item["args"] for item in recorded)


@pytest.mark.parametrize("args", [["--test-node", "tests/e2e/bot_test.py::TestWarmupShim"],
                                 ["--test-filter", "chat"], ["--test-node", "tests/e2e/"]])
def test_e2e_rejects_legacy_or_ambiguous_selection_before_external_commands(project, args):
    root, env = project
    shutil.copy(ROOT / "scripts/e2e-deploy-and-test.sh", root / "scripts")
    result = subprocess.run(["bash", str(root / "scripts/e2e-deploy-and-test.sh"), *args],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (root / "calls.jsonl").exists()


def test_phase3_rejects_v1_manifest_before_dependent_cdk(project):
    root, env = project
    manifest = root / "old-runtime.json"
    manifest.write_text(json.dumps({"platformVersion": "V1", "agentRuntimeId": "old-1234567890",
                                    "endpointName": "DEFAULT"}))
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--phase3",
                             "--manifest", str(manifest)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not any(item["tool"] == "cdk" for item in calls(project))
    verification = next(item["args"] for item in calls(project)
                        if item["tool"] == "deployment-python" and item["args"][1:2] == ["verify"])
    assert "--require-enrollment-ready" in verification


def test_e2e_selected_tests_use_uv_and_preserve_state(project):
    root, env = project
    shutil.copy(ROOT / "scripts/e2e-deploy-and-test.sh", root / "scripts")
    env["E2E_PYTHON"] = env["DEPLOY_PYTHON"]
    nodes = ["tests/e2e/selected_test.py::test_chat", "tests/e2e/selected_test.py::test_state"]
    original = (root / "cdk.json").read_bytes()
    result = subprocess.run(["bash", str(root / "scripts/e2e-deploy-and-test.sh"), "--skip-deploy",
                             "--test-node", nodes[0], "--test-node", nodes[1]],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = calls(project)
    assert (root / "cdk.json").read_bytes() == original
    assert not any(item["tool"] in {"aws", "curl", "cdk", "docker"} for item in recorded)
    verified = next(item["args"] for item in recorded if item["tool"] == "deployment-python"
                    and item["args"][1:2] == ["verify"])
    assert "--require-enrollment-ready" in verified
    tests = next(item["args"] for item in recorded if item["tool"] == "uv" and "pytest" in item["args"])
    assert tests == ["run", "--no-project", "--python", env["E2E_PYTHON"], "python",
                     "-m", "pytest", *nodes, "-v", "--tb=short"]
    assert not any("reset_session" in str(item) or "stop-runtime-session" in str(item) for item in recorded)


@pytest.mark.parametrize("fail_verify", [False, True])
def test_e2e_default_v2_classes_require_manifest_verification(project, fail_verify):
    root, env = project
    shutil.copy(ROOT / "scripts/e2e-deploy-and-test.sh", root / "scripts")
    env["E2E_PYTHON"] = env["DEPLOY_PYTHON"]
    if fail_verify:
        env["FAIL_VERIFY"] = "1"
    result = subprocess.run(["bash", str(root / "scripts/e2e-deploy-and-test.sh"), "--skip-deploy"],
                            env=env, capture_output=True, text=True)
    recorded = calls(project)
    test_calls = [item["args"] for item in recorded if item["tool"] == "uv" and "pytest" in item["args"]]
    if fail_verify:
        assert result.returncode == 2
        assert not test_calls
    else:
        assert result.returncode == 0, result.stderr
        assert test_calls[0][7:9] == ["tests/e2e/bot_test.py::TestSmoke",
                                     "tests/e2e/bot_test.py::TestMessageLifecycle"]
        assert "-k" not in test_calls[0]
    assert not any(item["tool"] in {"aws", "curl", "cdk", "docker"} for item in recorded)


@pytest.mark.parametrize("pytest_exit", [0, 7])
@pytest.mark.parametrize("notification_failure", [
    None, "FAIL_NOTIFICATION_SECRET", "FAIL_NOTIFICATION_JSON", "FAIL_NOTIFICATION_CURL",
])
def test_e2e_restores_notifications_log_and_summary_without_masking_test_status(
        project, pytest_exit, notification_failure):
    root, env = project
    shutil.copy(ROOT / "scripts/e2e-deploy-and-test.sh", root / "scripts")
    output = "tests/e2e/selected_test.py::test_chat PASSED\n"
    output += "tests/e2e/selected_test.py::test_optional SKIPPED\n"
    if pytest_exit:
        output += "tests/e2e/selected_test.py::test_failure FAILED\n"
        output += "tests/e2e/selected_test.py::test_setup ERROR\n"
    env.update(E2E_PYTHON=env["DEPLOY_PYTHON"], E2E_TELEGRAM_CHAT_ID='synthetic"chat',
               FAKE_PYTEST_OUTPUT=output, FAKE_PYTEST_EXIT=str(pytest_exit))
    if notification_failure:
        env[notification_failure] = "1"
    result = subprocess.run(["bash", str(root / "scripts/e2e-deploy-and-test.sh"), "--skip-deploy",
                             "--test-node", "tests/e2e/selected_test.py::test_chat"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == pytest_exit, result.stderr
    assert Path(env["E2E_RESULTS_FILE"]).read_text() == output
    assert output in result.stdout
    failures = int(bool(pytest_exit))
    assert f"E2E Results: PASSED=1 FAILED={failures} SKIPPED=1 ERRORS={failures}" in result.stdout
    assert f"Full results: {env['E2E_RESULTS_FILE']}" in result.stdout
    recorded = calls(project)
    secret_calls = [item for item in recorded if item["tool"] == "aws"]
    assert len(secret_calls) == 2
    assert all(item["args"][:2] == ["secretsmanager", "get-secret-value"] for item in secret_calls)
    notifications = [item["args"] for item in recorded if item["tool"] == "curl"]
    if notification_failure in {"FAIL_NOTIFICATION_SECRET", "FAIL_NOTIFICATION_JSON"}:
        assert not notifications
    else:
        assert len(notifications) == 2
        messages = [json.loads(args[args.index("-d") + 1]) for args in notifications]
        assert all(message["chat_id"] == 'synthetic"chat' for message in messages)
        assert "E2E Pipeline started" in messages[0]["text"]
        assert "E2E Pipeline complete" in messages[1]["text"]
        assert f"Passed: 1 | Failed: {failures} | Skipped: 1 | Errors: {failures}" in messages[1]["text"]
        assert ("test_failure" in messages[1]["text"]) == bool(pytest_exit)
        assert "tests failed" in messages[1]["text"] if pytest_exit else "tests passed" in messages[1]["text"]
    verified = next(i for i, item in enumerate(recorded)
                    if item["tool"] == "deployment-python" and item["args"][1:2] == ["verify"])
    tested = next(i for i, item in enumerate(recorded)
                  if item["tool"] == "deployment-python" and item["args"][:2] == ["-m", "pytest"])
    assert verified < tested
    assert not any(item["tool"] in {"cdk", "docker"} for item in recorded)


@pytest.mark.parametrize("pytest_exit", [0, 7])
def test_e2e_uses_pytest_exit_code_when_tee_also_fails(project, pytest_exit):
    root, env = project
    shutil.copy(ROOT / "scripts/e2e-deploy-and-test.sh", root / "scripts")
    (root / "bin/tee").symlink_to(root / "bin/fake")
    env.update(E2E_PYTHON=env["DEPLOY_PYTHON"], FAKE_PYTEST_EXIT=str(pytest_exit),
               FAKE_TEE_EXIT="23")
    result = subprocess.run(["bash", str(root / "scripts/e2e-deploy-and-test.sh"), "--skip-deploy"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == pytest_exit, result.stderr


@pytest.mark.parametrize("enabled", [True, False])
def test_wrapper_passes_explicit_feature_clear_plan(project, enabled):
    root, env = project
    config = json.loads((root / "cdk.json").read_text())
    config["context"].update(enable_guardrails=enabled, enable_browser=enabled,
                             enable_gateway=enabled, subagent_model_id="model" if enabled else "")
    (root / "cdk.json").write_text(json.dumps(config))
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only",
                             "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    configured = next(item["configuration"] for item in calls(project)
                      if item["tool"] == "deployment-python" and item["args"][1:2] == ["deploy"])
    keys = {"BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION", "BROWSER_IDENTIFIER",
            "AGENTCORE_GATEWAY_URL", "SUBAGENT_BEDROCK_MODEL_ID"}
    if enabled:
        assert keys <= configured["environmentVariables"].keys()
        assert not keys.intersection(configured.get("unsetEnvironmentVariables", []))
    else:
        assert keys <= set(configured["unsetEnvironmentVariables"])
        assert not keys.intersection(configured["environmentVariables"])
        assert not any("Stacks[0].Outputs[?OutputKey=='BrowserIdentifier'].OutputValue" in item["args"]
                       for item in calls(project))


@pytest.mark.parametrize("failure,output,error", [
    (True, "", "AccessDenied"),
    (False, "", "BROWSER_IDENTIFIER"),
    (False, "None", "BROWSER_IDENTIFIER"),
    (False, " \t", "BrowserIdentifier"),
    (False, "not-a-browser-id", "BrowserIdentifier"),
])
def test_enabled_browser_lookup_failure_stops_before_runtime_configuration(project, failure, output, error):
    root, env = project
    if failure:
        env["FAIL_BROWSER_LOOKUP"] = "1"
    env["BROWSER_OUTPUT"] = output
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only",
                             "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    if failure:
        assert result.returncode == 37
    assert error in result.stderr
    recorded = calls(project)
    assert not any(item["args"][1:2] == ["deploy"] or item["args"][0].endswith("runtime_config.py")
                   or item["args"][0].endswith("runtime_image.py") for item in recorded)
    assert not any(item["tool"] in {"cdk", "docker"} for item in recorded)


@pytest.mark.parametrize("identifier", ["aws.browser.v1", "browser_custom-abcdefghij"])
def test_enabled_browser_keeps_valid_identifiers(project, identifier):
    root, env = project
    env["BROWSER_OUTPUT"] = identifier
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--runtime-only",
                             "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    configured = next(item["configuration"] for item in calls(project)
                      if item["tool"] == "deployment-python" and item["args"][1:2] == ["deploy"])
    assert configured["environmentVariables"]["BROWSER_IDENTIFIER"] == identifier
    assert "BROWSER_IDENTIFIER" not in configured.get("unsetEnvironmentVariables", [])


def test_v2_verifies_enrollment_before_dependent_stacks(project):
    root, env = project
    original = (root / "cdk.json").read_bytes()
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"),
                             "--endpoint-name", "release_1",
                             "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = calls(project)
    deployed = next(item["args"] for item in recorded
                    if item["tool"] == "deployment-python" and item["args"][1:2] == ["deploy"])
    assert not {"--active-runtime-id", "--source-runtime-id", "--runtime-id"}.intersection(deployed)
    assert deployed[deployed.index("--endpoint-name") + 1] == "release_1"
    cdk_calls = [(i, item["args"]) for i, item in enumerate(recorded) if item["tool"] == "cdk"]
    assert "runtime_manifest=" in cdk_calls[0][1]
    assert "runtime_manifest=.runtime/runtime-v2.json" in cdk_calls[-1][1]
    verification = next((i, item["args"]) for i, item in enumerate(recorded)
                        if item["tool"] == "deployment-python" and item["args"][1:2] == ["verify"])
    assert "--require-enrollment-ready" in verification[1]
    assert verification[0] < cdk_calls[-1][0]
    assert (root / "cdk.json").read_bytes() == original


def test_phase1_clears_configured_manifest_for_foundation_only(project):
    root, env = project
    config = json.loads((root / "cdk.json").read_text())
    config["context"]["runtime_manifest"] = ".runtime/existing.json"
    (root / "cdk.json").write_text(json.dumps(config))
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--phase1"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    cdk_calls = [item["args"] for item in calls(project) if item["tool"] == "cdk"]
    assert len(cdk_calls) == 1
    assert "runtime_manifest=" in cdk_calls[0]
    assert "OpenClawRouter" not in cdk_calls[0]


def test_failed_enrollment_verification_never_calls_dependent_cdk(project):
    root, env = project
    env["FAIL_VERIFY"] = "1"
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "--phase3"], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not any(item["tool"] == "cdk" for item in calls(project))


@pytest.mark.parametrize("mode", [[], ["--phase1"], ["--phase3"], ["--cdk-only"]])
def test_enrollment_cannot_skip_chat_warmup(project, mode):
    root, env = project
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), *mode, "--skip-warmup"],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "requires successful chat warmup" in result.stderr
    assert not (root / "calls.jsonl").exists()


def test_wrapper_omitted_endpoint_reuses_real_controller_manifest(project):
    root, env = project
    env["CONTROLLER_STATE"] = str(root / "controller-state.json")
    command = ["bash", str(root / "scripts/deploy.sh"), "--runtime-only",
               "--image-uri", "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v1"]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    manifest_path = root / ".runtime/runtime-v2.json"
    first = json.loads(manifest_path.read_text())
    assert (first["endpointName"], first["agentRuntimeVersion"]) == ("release_1", "1")
    config = json.loads((root / "cdk.json").read_text())
    config["context"]["image_version"] = "71"
    (root / "cdk.json").write_text(json.dumps(config))
    result = subprocess.run(command + ["--endpoint-name", "release_2"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    latest = json.loads(manifest_path.read_text())
    assert (latest["endpointName"], latest["agentRuntimeVersion"]) == ("release_2", "2")
    before = json.loads((root / "controller-state.json").read_text())
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    resumed = json.loads(manifest_path.read_text())
    after = json.loads((root / "controller-state.json").read_text())
    assert resumed["endpointName"] == "release_2"
    assert resumed["agentRuntimeVersion"] == "2"
    assert resumed["warmup"] == latest["warmup"]
    assert after["invokes"] == before["invokes"]
    assert all(op in {"get", "get-endpoint"} for op, _ in after["calls"][len(before["calls"]):])
    config["context"]["image_version"] = "72"
    (root / "cdk.json").write_text(json.dumps(config))
    original = manifest_path.read_bytes()
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "new --endpoint-name" in result.stderr
    assert manifest_path.read_bytes() == original
    rejected = json.loads((root / "controller-state.json").read_text())
    assert rejected["invokes"] == after["invokes"]
    assert all(op in {"get", "get-endpoint"} for op, _ in rejected["calls"][len(after["calls"]):])


@pytest.mark.parametrize("existing", ["legacy", "other", "same"])
def test_phase3_real_controller_protects_existing_callers(project, existing):
    root, env = project
    env["CONTROLLER_STATE"] = str(root / "controller-state.json")
    script = str(root / "scripts/deploy.sh")
    prepared = subprocess.run(
        ["bash", script, "--runtime-only", "--image-uri",
         "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v1"],
        env=env, capture_output=True, text=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    (root / "calls.jsonl").unlink()
    env["EXISTING_CALLER"] = existing
    result = subprocess.run(["bash", script, "--phase3"], env=env, capture_output=True, text=True)
    cdk_calls = [item for item in calls(project) if item["tool"] == "cdk"]
    assert (result.returncode == 0) == (existing == "same"), result.stderr
    assert bool(cdk_calls) == (existing == "same")
