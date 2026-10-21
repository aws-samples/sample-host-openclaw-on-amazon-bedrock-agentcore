"""scripts/undeploy.sh against a fake AWS CLI: confirmation, suffix naming, order.

No AWS calls: `aws` and `python3` resolve to local fakes/the test interpreter.

    pytest tests/test_undeploy.py -v
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, REGION = "123456789012", "us-west-2"

FAKE_AWS = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
state_path = pathlib.Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
state["calls"].append(args)
def save():
    state_path.write_text(json.dumps(state))
def opt(name):
    return args[args.index(name) + 1] if name in args else None
out = ""
code = 0
if args[:2] == ["sts", "get-caller-identity"]:
    out = os.environ["FAKE_ACCOUNT"]
elif args[:2] == ["configure", "get"]:
    code = 1
elif args[:2] == ["cloudformation", "describe-stacks"]:
    stack = opt("--stack-name")
    if stack not in state["stacks"]:
        code = 255
    elif "StackStatus" in opt("--query"):
        out = "CREATE_COMPLETE"
    else:
        out = "sg-12345678"
elif args[:2] == ["cloudformation", "describe-stack-resources"]:
    out = "vpc-12345678"
elif args[:2] == ["cloudformation", "delete-stack"]:
    state["stacks"].remove(opt("--stack-name"))
elif args[:2] == ["bedrock-agentcore-control", "list-agent-runtimes"]:
    name = os.environ.get("FAKE_RUNTIME_NAME", "")
    out = state["runtime"] if state["runtime"] and name in opt("--query") else "None"
elif args[:2] == ["bedrock-agentcore-control", "list-agent-runtime-endpoints"]:
    out = "release_1\trelease_2"
elif args[:2] == ["bedrock-agentcore-control", "delete-agent-runtime"]:
    state["runtime"] = ""
elif args[:2] == ["bedrock-agentcore-control", "get-agent-runtime"]:
    if state["runtime"]:
        out = "READY"
    else:
        code = 254
elif args[:2] == ["ec2", "describe-network-interfaces"]:
    out = ""
save()
if out:
    print(out)
sys.exit(code)
'''

ALL = ["OpenClawTokenMonitoring", "OpenClawCron", "OpenClawRouter", "OpenClawAgentCore",
       "OpenClawGateway", "OpenClawGuardrails", "OpenClawObservability", "OpenClawVpc", "OpenClawSecurity"]


@pytest.fixture
def project(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/undeploy.sh", scripts)
    shutil.copytree(ROOT / "scripts/lib", scripts / "lib")
    (tmp_path / "cdk.json").write_text(json.dumps({"context": {"region": REGION}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_AWS)
    fake.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path), "FAKE_STATE": str(tmp_path / "state.json"),
        "FAKE_ACCOUNT": ACCOUNT, "POLL_INTERVAL_SECONDS": "0", "DESTROY_TIMEOUT_SECONDS": "5",
    }
    return tmp_path, env


def seed(root, stacks, runtime="rt-1234567890"):
    (root / "state.json").write_text(json.dumps({"stacks": stacks, "runtime": runtime, "calls": []}))


def state(root):
    return json.loads((root / "state.json").read_text())


def run(root, env, *args, stdin=None):
    return subprocess.run(["bash", str(root / "scripts/undeploy.sh"), *args], env=env, cwd=root,
                          input=stdin, capture_output=True, text=True)


def mutating(calls):
    return [c for c in calls if c[1].startswith("delete")]


def test_without_terminal_or_yes_nothing_is_deleted(project):
    root, env = project
    seed(root, list(ALL))
    result = run(root, env, stdin="default\n")
    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert mutating(state(root)["calls"]) == []
    assert "This will delete:" in result.stdout


def test_yes_deletes_in_dependency_order_and_keeps_security(project):
    root, env = project
    seed(root, list(ALL))
    env["FAKE_RUNTIME_NAME"] = "openclaw_agent_v2"
    result = run(root, env, "--yes")
    assert result.returncode == 0, result.stderr
    calls = mutating(state(root)["calls"])
    order = [c[c.index("--stack-name") + 1] if "--stack-name" in c else f"{c[1]}:{c[c.index('--agent-runtime-id') + 1]}"
             for c in calls]
    assert order == [
        "OpenClawTokenMonitoring", "OpenClawCron", "OpenClawRouter",
        "delete-agent-runtime-endpoint:rt-1234567890", "delete-agent-runtime-endpoint:rt-1234567890",
        "delete-agent-runtime:rt-1234567890",
        "OpenClawAgentCore", "OpenClawGateway", "OpenClawGuardrails", "OpenClawObservability", "OpenClawVpc",
    ]
    assert state(root)["stacks"] == ["OpenClawSecurity"]


def test_env_suffix_targets_only_suffixed_names(project):
    root, env = project
    (root / ".env.dev").write_text("CDK_DEFAULT_REGION=us-west-2\n")
    seed(root, [f"{s}-dev" for s in ALL] + list(ALL))
    env["FAKE_RUNTIME_NAME"] = "openclaw_agent_v2_dev"
    result = run(root, env, "--env", "dev", "--all", "--yes")
    assert result.returncode == 0, result.stderr
    assert state(root)["stacks"] == ALL
    queries = [c[c.index("--query") + 1] for c in state(root)["calls"] if c[1] == "list-agent-runtimes"]
    assert queries and "openclaw_agent_v2_dev" in queries[0]


def test_bucket_deletion_needs_its_own_confirmation(project):
    root, env = project
    seed(root, [], runtime="")
    script = f"bash {root / 'scripts/undeploy.sh'} --delete-user-files-bucket"
    # `script` gives the prompt a terminal; answer the first prompt, refuse the second.
    if not shutil.which("script"):
        pytest.skip("util-linux script(1) is not available")
    result = subprocess.run(["script", "-qec", script, "/dev/null"], env=env, cwd=root,
                            input="default\nnot-the-bucket\n", capture_output=True, text=True)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    # The first prompt was answered correctly; the bucket prompt stopped the run.
    assert "Type the bucket name" in output
    assert "Aborted: nothing was deleted." in output
    assert not any(c[0] == "s3api" and c[1].startswith("delete") for c in state(root)["calls"])


def test_invalid_env_name_stops_before_aws(project):
    root, env = project
    seed(root, list(ALL))
    result = run(root, env, "--env", "Prod_1", "--yes")
    assert result.returncode != 0
    assert state(root)["calls"] == []
