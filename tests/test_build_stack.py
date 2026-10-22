"""Offline tests for the opt-in CodeBuild image build (stacks/build_stack.py, scripts/codebuild-push.sh).

No AWS calls: synth uses fixed account/region/AZ context and the script runs
against fake aws/uv/deployment-python executables.

    pytest tests/test_build_stack.py -v
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

import aws_cdk as cdk
import cdk_nag
import pytest
from aws_cdk.assertions import Annotations, Match, Template

from stacks.build_stack import BuildStack

_ACCOUNT = "123456789012"
_REGION = "us-west-2"
_REPO = pathlib.Path(__file__).resolve().parent.parent


def _run_app(tmp_path, **extra):
    """Run app.py the way `cdk synth` does, hermetically; return the stack names and output dir."""
    ctx = dict(json.loads((_REPO / "cdk.json").read_text())["context"])
    ctx.update({"account": _ACCOUNT, "region": _REGION,
                "availability_zones": [f"{_REGION}a", f"{_REGION}b"]})
    ctx.update(extra)
    out = tmp_path / "cdk.out"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "CDK_OUTDIR": str(out),
        "CDK_CONTEXT_JSON": json.dumps(ctx),
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DEFAULT_ACCOUNT": _ACCOUNT,
        "CDK_DEFAULT_REGION": _REGION,
    }
    for k in ("JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR", "NODE_OPTIONS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    subprocess.run([sys.executable, "app.py"], cwd=_REPO, env=env, check=True, capture_output=True)
    return {p.name[: -len(".template.json")] for p in out.glob("*.template.json")}, out


def _feature_flags():
    ctx = json.loads((_REPO / "cdk.json").read_text())["context"]
    return {k: v for k, v in ctx.items() if k.startswith("@aws-cdk/")}


def _synth_build(repo="openclaw-bridge", env=True, **context):
    app = cdk.App(context={**_feature_flags(), **context})
    stack = BuildStack(app, "OpenClawBuild", ecr_repo_name=repo,
                       env=cdk.Environment(account=_ACCOUNT, region=_REGION) if env else None)
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))
    return stack, Template.from_stack(stack)


# --------------------------------------------------------------------------- app wiring


@pytest.mark.parametrize("flag", [None, False, "false"])
def test_flag_off_leaves_build_stack_out_of_the_app(tmp_path, flag):
    stacks, _ = _run_app(tmp_path, **({} if flag is None else {"enable_codebuild": flag}))
    assert "OpenClawBuild" not in stacks
    assert len(stacks) == 8


@pytest.mark.parametrize("flag", [True, "true"])
def test_flag_on_adds_build_stack(tmp_path, flag):
    stacks, out = _run_app(tmp_path, enable_codebuild=flag)
    assert "OpenClawBuild" in stacks
    assert len(stacks) == 9
    tpl = json.loads((out / "OpenClawBuild.template.json").read_text())
    assert set(tpl["Outputs"]) >= {"BuildProjectName", "BuildSourceBucketName", "EcrRepositoryName"}
    assert tpl["Outputs"]["EcrRepositoryName"]["Value"] == "openclaw-bridge"


# --------------------------------------------------------------------------- stack


def test_ecr_push_is_scoped_to_the_bridge_repository():
    _, tpl = _synth_build()
    statements = [s for p in tpl.find_resources("AWS::IAM::Policy").values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    ecr = [s for s in statements if any(a.startswith("ecr:") for a in
                                        (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))]
    push = [s for s in ecr if "ecr:PutImage" in s["Action"]]
    assert len(push) == 1
    assert push[0]["Resource"] == f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/openclaw-bridge"
    wildcard = [s for s in ecr if s["Resource"] == "*"]
    assert [s["Action"] for s in wildcard] == ["ecr:GetAuthorizationToken"]


def test_project_builds_arm64_and_requires_a_per_build_tag():
    _, tpl = _synth_build()
    tpl.has_resource_properties("AWS::CodeBuild::Project", {
        "Environment": Match.object_like({
            "Type": "ARM_CONTAINER",
            "PrivilegedMode": True,
            "EnvironmentVariables": Match.array_with([
                {"Name": "IMAGE_TAG", "Type": "PLAINTEXT", "Value": ""},
            ]),
        }),
    })
    project = next(iter(tpl.find_resources("AWS::CodeBuild::Project").values()))
    spec = project["Properties"]["Source"]["BuildSpec"]
    assert "--platform linux/arm64" in spec
    assert "{{.Architecture}}" in spec and "= arm64" in spec
    assert "post_build" not in spec


@pytest.mark.parametrize("env", [True, False], ids=["with-env", "env-agnostic"])
def test_no_cdk_nag_errors(env):
    stack, _ = _synth_build(env=env)
    Annotations.from_stack(stack).has_no_error("*", Match.any_value())


def test_rejects_repository_the_execution_role_cannot_pull():
    with pytest.raises(ValueError, match="execution-role prefix"):
        _synth_build(repo="some-other-repo")


def _fixed_names(tpl):
    return (
        next(iter(tpl.find_resources("AWS::CodeBuild::Project").values()))["Properties"]["Name"],
        next(iter(tpl.find_resources("AWS::S3::Bucket").values()))["Properties"]["BucketName"],
        next(iter(tpl.find_resources("AWS::Logs::LogGroup").values()))["Properties"]["LogGroupName"],
    )


def test_names_are_unchanged_without_an_environment_suffix(monkeypatch):
    monkeypatch.delenv("OPENCLAW_ENV_SUFFIX", raising=False)
    _, tpl = _synth_build()
    assert _fixed_names(tpl) == ("openclaw-bridge-build", f"openclaw-build-source-{_ACCOUNT}-{_REGION}",
                                 "/openclaw/codebuild/bridge-build")


def test_environment_suffix_reaches_every_fixed_name(monkeypatch):
    monkeypatch.delenv("OPENCLAW_ENV_SUFFIX", raising=False)
    stack, tpl = _synth_build(environment_suffix="dev")
    assert _fixed_names(tpl) == ("openclaw-bridge-build-dev", f"openclaw-build-source-{_ACCOUNT}-{_REGION}-dev",
                                 "/openclaw/codebuild/bridge-build-dev")
    Annotations.from_stack(stack).has_no_error("*", Match.any_value())


# --------------------------------------------------------------------------- script

_FAKE = r'''
import json, os, pathlib, re, shutil, sys
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps({"tool": tool, "args": args}) + "\n")
if tool == "uv":
    os.execv(args[3], [args[3], *args[5:]])
if tool == "deployment-python":
    if args[0] == "-c":
        os.execv(sys.executable, [sys.executable, *args])
    if args[0].endswith("runtime_image.py"):
        print("123456789012.dkr.ecr.us-west-2.amazonaws.com/" + args[args.index("--name") + 1])
    sys.exit(0)
if tool == "aws":
    if args[:2] == ["cloudformation", "describe-stacks"]:
        key = re.search("OutputKey=='([^']+)'", args[args.index("--query") + 1]).group(1)
        print({"BuildProjectName": "openclaw-bridge-build",
               "BuildSourceBucketName": "openclaw-build-source-bucket",
               "EcrRepositoryName": os.environ.get("STACK_REPOSITORY", "openclaw-bridge")}[key])
    elif args[:2] == ["s3", "cp"]:
        shutil.copy(args[2], os.environ["UPLOADED_ZIP"])
    elif args[:2] == ["codebuild", "start-build"]:
        print("openclaw-bridge-build:00000000-0000-0000-0000-000000000000")
    elif args[:2] == ["codebuild", "batch-get-builds"]:
        print(os.environ.get("BUILD_STATUS", "SUCCEEDED") + "\tCOMPLETED")
'''


@pytest.fixture
def script_env(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(_REPO / "scripts/codebuild-push.sh", tmp_path / "scripts")
    shutil.copy(_REPO / "scripts/env_suffix.py", tmp_path / "scripts")
    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge/Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "bridge/patch-openclaw.test.js").write_text("// copied by the Dockerfile\n")
    (tmp_path / "bridge/node_modules").mkdir()
    (tmp_path / "bridge/node_modules/dep.js").write_text("")
    (tmp_path / "cdk.json").write_text(json.dumps({"context": {"region": "", "image_version": "70"}}))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "fake"
    fake.write_text(f"#!{sys.executable}\n" + _FAKE)
    fake.chmod(0o755)
    for name in ("aws", "uv", "deployment-python"):
        (binaries / name).symlink_to(fake)
    env = {
        **os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "CALL_LOG": str(tmp_path / "calls.jsonl"), "UPLOADED_ZIP": str(tmp_path / "uploaded.zip"),
        "CDK_DEFAULT_ACCOUNT": _ACCOUNT, "CDK_DEFAULT_REGION": _REGION,
        "DEPLOY_PYTHON": str(binaries / "deployment-python"),
    }
    env.pop("ECR_REPOSITORY", None)
    env.pop("OPENCLAW_ENV_SUFFIX", None)
    return tmp_path, env


def _calls(root):
    path = root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _run_script(root, env, *args):
    return subprocess.run(["bash", str(root / "scripts/codebuild-push.sh"), *args],
                          env=env, capture_output=True, text=True)


def test_script_prints_only_the_image_uri_for_deploy_sh(script_env):
    root, env = script_env
    result = _run_script(root, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge:v70\n"
    assert "BUILD_MODE=codebuild ./scripts/deploy.sh --image-uri" in result.stderr
    start = next(c["args"] for c in _calls(root) if c["args"][:2] == ["codebuild", "start-build"])
    assert "name=IMAGE_TAG,value=v70,type=PLAINTEXT" in start


def _stack_names_read(root):
    return {c["args"][c["args"].index("--stack-name") + 1] for c in _calls(root)
            if c["args"][:2] == ["cloudformation", "describe-stacks"]}


@pytest.mark.parametrize("suffix, stack", [(None, "OpenClawBuild"), ("", "OpenClawBuild"),
                                           ("dev", "OpenClawBuild-dev"), (" Team-A ", "OpenClawBuild-team-a")])
def test_script_reads_the_stack_for_the_environment_suffix(script_env, suffix, stack):
    root, env = script_env
    if suffix is not None:
        env["OPENCLAW_ENV_SUFFIX"] = suffix
    result = _run_script(root, env)
    assert result.returncode == 0, result.stderr
    assert _stack_names_read(root) == {stack}


@pytest.mark.parametrize("suffix", ["bad_name", "x" * 13])
def test_script_rejects_an_invalid_suffix_before_any_aws_call(script_env, suffix):
    root, env = script_env
    env["OPENCLAW_ENV_SUFFIX"] = suffix
    result = _run_script(root, env)
    assert result.returncode != 0
    assert "OPENCLAW_ENV_SUFFIX" in result.stderr
    assert not any(c["tool"] == "aws" for c in _calls(root))


def test_script_version_argument_overrides_cdk_json(script_env):
    root, env = script_env
    result = _run_script(root, env, "71")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("/openclaw-bridge:v71")


def test_script_keeps_test_files_the_dockerfile_copies(script_env):
    root, env = script_env
    assert _run_script(root, env).returncode == 0
    names = zipfile.ZipFile(root / "uploaded.zip").namelist()
    assert "patch-openclaw.test.js" in names
    assert "Dockerfile" in names
    assert not any(n.startswith("node_modules/") for n in names)


def test_script_refuses_a_repository_the_stack_cannot_push_to(script_env):
    root, env = script_env
    env["ECR_REPOSITORY"] = "openclaw-bridge-other"
    result = _run_script(root, env)
    assert result.returncode != 0
    assert "ECR_REPOSITORY" in result.stderr
    assert not any(c["args"][:2] in (["s3", "cp"], ["codebuild", "start-build"]) for c in _calls(root))


def test_script_fails_when_the_build_fails(script_env):
    root, env = script_env
    env["BUILD_STATUS"] = "FAILED"
    result = _run_script(root, env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "FAILED" in result.stderr


@pytest.mark.parametrize("version", ["../x", "a b", "-1"])
def test_script_rejects_an_invalid_version_before_any_aws_call(script_env, version):
    root, env = script_env
    result = _run_script(root, env, version)
    assert result.returncode != 0
    assert not any(c["tool"] == "aws" for c in _calls(root))
