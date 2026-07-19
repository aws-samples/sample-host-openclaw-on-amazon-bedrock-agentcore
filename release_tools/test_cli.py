from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from release_tools.contracts import (
    ProductionObservationConfigV1,
    StagingTransactionV1,
    canonical_json_bytes,
    write_new_contract,
)
from release_tools import cli as release_cli
from release_tools.transaction import TransactionJournal


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "staging-release.py"
ACCOUNT = "123456789012"
REGION = "eu-west-1"


def test_production_observation_clients_ignore_operator_endpoint_and_proxy_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[tuple[str, object]] = []

    class FakeSession:
        def __init__(self, *, region_name: str) -> None:
            assert region_name == REGION

        def client(self, service: str, **kwargs: object) -> object:
            assert kwargs["region_name"] == REGION
            clients.append((service, kwargs["config"]))
            return object()

    sentinel = object()
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=FakeSession))
    monkeypatch.setattr(
        release_cli,
        "compose_production_evidence",
        lambda **kwargs: sentinel,
    )
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8")
    config = _observation_config(
        {"commit": "a" * 40, "tree": "b" * 40}
    )

    assert release_cli._production_composer(config) is sentinel
    assert [service for service, _ in clients] == [
        "ecr",
        "bedrock-agentcore-control",
        "cloudformation",
    ]
    for _, client_config in clients:
        assert client_config.ignore_configured_endpoint_urls is True
        assert client_config.proxies == {}


def test_account_discovery_ignores_an_ambient_path_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted" / "aws"
    shadow = tmp_path / "shadow" / "aws"
    _write_executable(trusted, "#!/bin/sh\nexit 0\n")
    _write_executable(shadow, "#!/bin/sh\nexit 99\n")
    monkeypatch.setattr(
        release_cli,
        "TRUSTED_AWS_CLI_CANDIDATES",
        (trusted,),
    )
    monkeypatch.setenv("PATH", f"{shadow.parent}:/usr/bin:/bin")

    assert release_cli._trusted_aws_cli() == trusted.resolve()

    trusted.chmod(0o777)
    with pytest.raises(release_cli.ReleaseCliError, match="trusted absolute"):
        release_cli._trusted_aws_cli()


def test_git_identity_ignores_path_shadow_and_git_repository_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "candidate")
    foreign = _fixture(tmp_path / "foreign")
    marker = tmp_path / "shadow-ran"
    shadow = tmp_path / "shadow" / "git"
    _write_executable(
        shadow,
        f"#!/bin/sh\nprintf shadow\n: > {str(marker)!r}\n",
    )
    monkeypatch.setenv("PATH", f"{shadow.parent}:/usr/bin:/bin")
    monkeypatch.setenv("GIT_DIR", str(foreign["repo"] / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign["repo"]))

    assert (
        release_cli._git(fixture["repo"], "rev-parse", "HEAD")
        == fixture["commit"]
    )
    assert not marker.exists()


def test_release_environment_supports_fixed_aws_login_profile_and_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_home = tmp_path / "login-home"
    monkeypatch.setattr(release_cli, "_LOGIN_HOME", login_home)
    monkeypatch.setenv("AWS_PROFILE", "personal-operator-release")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "poison-config"))
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "poison-credentials")
    )
    monkeypatch.setenv(
        "AWS_LOGIN_CACHE_DIRECTORY", str(tmp_path / "poison-login-cache")
    )

    environment = release_cli._sanitized_environment(ACCOUNT, REGION)

    assert environment["AWS_PROFILE"] == "personal-operator-release"
    assert environment["AWS_CONFIG_FILE"] == str(login_home / ".aws" / "config")
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == str(
        login_home / ".aws" / "credentials"
    )
    assert environment["AWS_LOGIN_CACHE_DIRECTORY"] == str(
        login_home / ".aws" / "login" / "cache"
    )
    assert environment["AWS_SDK_LOAD_CONFIG"] == "1"


def test_live_observer_runs_inside_the_exact_account_discovery_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::999999999999:role/poison")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
    environment = release_cli._sanitized_environment(ACCOUNT, REGION)

    with release_cli._scoped_release_environment(environment):
        assert dict(os.environ) == environment

    assert os.environ["AWS_ROLE_ARN"].endswith("role/poison")
    assert os.environ["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9"


def _observation_config(fixture: dict[str, object]) -> ProductionObservationConfigV1:
    return ProductionObservationConfigV1.from_mapping(
        {
            "schema": ProductionObservationConfigV1.SCHEMA,
            "sourceCommit": fixture["commit"],
            "sourceTree": fixture["tree"],
            "account": ACCOUNT,
            "region": REGION,
            "buildContext": "bridge",
            "builderId": "https://personal-operator.invalid/builders/bridge-v1",
            "builderInputs": ["sha256:" + "f" * 64],
            "runtimeSubnetIds": [
                "subnet-00000000000000001",
                "subnet-00000000000000002",
            ],
            "runtimeSecurityGroupIds": ["sg-00000000000000001"],
            "runtimeEnvironmentVariables": {
                "AWS_DEFAULT_REGION": REGION,
                "AWS_REGION": REGION,
                "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
                "S3_USER_FILES_BUCKET": "personal-operator-user-files-123456789012",
                "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                    "workspace-credential-broker"
                ),
                "WORKSPACE_SYNC_INTERVAL_MS": "300000",
            },
            "runtimeIdleSessionTimeout": 1800,
            "runtimeMaxLifetime": 28800,
            "foundationStackTemplateParameterDigests": {
                name: hashlib.sha256(f"foundation:{name}".encode()).hexdigest()
                for name in (
                    "OpenClawVpc",
                    "OpenClawSecurity",
                    "OpenClawGuardrails",
                    "PersonalOperatorCapabilities",
                    "PersonalOperatorCompute",
                    "OpenClawAgentCore",
                    "OpenClawObservability",
                )
            },
            "runtimeStackTemplateParameterDigest": "6" * 64,
            "consumerStackTemplateParameterDigests": {
                name: hashlib.sha256(f"consumer:{name}".encode()).hexdigest()
                for name in (
                    "OpenClawRouter",
                    "PersonalOperatorWeb",
                    "OpenClawCron",
                    "PersonalOperatorScheduler",
                )
            },
            "consumerChangeSetContentDigests": {
                name: hashlib.sha256(f"change-set:{name}".encode()).hexdigest()
                for name in (
                    "OpenClawRouter",
                    "PersonalOperatorWeb",
                    "OpenClawCron",
                    "PersonalOperatorScheduler",
                )
            },
            "foundationStackRequestDigests": {
                name: hashlib.sha256(
                    f"foundation-request:{name}".encode()
                ).hexdigest()
                for name in (
                    "OpenClawVpc",
                    "OpenClawSecurity",
                    "OpenClawGuardrails",
                    "PersonalOperatorCapabilities",
                    "PersonalOperatorCompute",
                    "OpenClawAgentCore",
                    "OpenClawObservability",
                )
            },
            "runtimeStackRequestDigest": "7" * 64,
            "consumerStackRequestDigests": {
                name: hashlib.sha256(
                    f"consumer-request:{name}".encode()
                ).hexdigest()
                for name in (
                    "OpenClawRouter",
                    "PersonalOperatorWeb",
                    "OpenClawCron",
                    "PersonalOperatorScheduler",
                )
            },
        }
    )


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _operation_sha256(fixture: dict[str, object]) -> str:
    config_path = Path(
        str(fixture["journal"]) + ".production-observation.json"
    )
    config = ProductionObservationConfigV1.from_bytes(config_path.read_bytes())
    return release_cli._reviewed_operation_sha256(
        fixture["driver"].read_bytes(),
        config,
    )


def _fixture(
    tmp_path: Path,
    *,
    include_observation_config: bool = True,
) -> dict[str, object]:
    repository = tmp_path / "repo"
    repository.mkdir(parents=True)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"], cwd=repository, check=True
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True
    ).strip()
    rollback = (
        f"rollback:v1:{ACCOUNT}:{REGION}:{commit}:sha256:" + "9" * 64
    )

    call_log = tmp_path / "calls.log"
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "aws",
        f"""#!{sys.executable}
from pathlib import Path
import sys

arguments = " ".join(sys.argv[1:])
with Path({str(call_log)!r}).open("a", encoding="utf-8") as log:
    log.write(f"aws <{{arguments}}>\\n")
if arguments == "sts get-caller-identity --query Account --output text --region eu-west-1":
    print({ACCOUNT!r})
else:
    print(f"unexpected aws command: {{arguments}}", file=sys.stderr)
    raise SystemExit(97)
""",
    )
    driver = tmp_path / "phase-driver"
    _write_executable(
        driver,
        f"""#!{sys.executable}
import argparse
import hashlib
import json
import os
import pathlib
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--mode", required=True)
parser.add_argument("--phase", required=True)
parser.add_argument("--journal", required=True)
parser.add_argument("--transaction-id", required=True)
parser.add_argument("--source-commit", required=True)
parser.add_argument("--source-tree", required=True)
parser.add_argument("--account", required=True)
parser.add_argument("--region", required=True)
parser.add_argument("--operation-sha256", required=True)
args = parser.parse_args()
control_path = pathlib.Path(args.journal + ".driver-control.json")
control = json.loads(control_path.read_text(encoding="utf-8"))
with pathlib.Path({str(call_log)!r}).open("a", encoding="utf-8") as log:
    log.write(
        f"driver {{args.mode}} <{{args.phase}}> region="
        f"<{{os.environ.get('CDK_DEFAULT_REGION', '')}}>"
        f"/<{{os.environ.get('AWS_REGION', '')}}>"
        f"/<{{os.environ.get('AWS_DEFAULT_REGION', '')}}>\\n"
    )
if args.mode == "mutate":
    if control.get("RELEASE_FAIL_PHASE") == args.phase:
        raise SystemExit(75)
    value = {{}} if control.get("RELEASE_BAD_ACK_PHASE") == args.phase else {{"dispatched": True}}
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    raise SystemExit(0)
if args.mode != "observe":
    raise SystemExit(76)
if control.get("RELEASE_FAIL_OBSERVE_PHASE") == args.phase:
    raise SystemExit(77)
outcome = control.get("RELEASE_OBSERVE_OUTCOME", "PERSISTED")
evidence = {{}}
digest = "sha256:" + "0" * 64
image_uri = (
    f"{{args.account}}.dkr.ecr.{{args.region}}.amazonaws.com/"
    f"personal-operator/bridge@{{digest}}"
)
context = {{
    "account": args.account,
    "region": args.region,
    "runtimeArn": (
        f"arn:aws:bedrock-agentcore:{{args.region}}:{{args.account}}:"
        "agent/12345678-1234-1234-1234-123456789abc:7"
    ),
    "runtimeEndpointId": "ReleaseEndpoint-ABCDEFGHIJ",
    "runtimeEndpointName": f"release_{{args.source_commit}}",
    "runtimeId": "Runtime-ABCDEFGHIJ",
    "runtimeImageUri": image_uri,
    "runtimeVersion": "7",
    "schema": "personal-operator.runtime-context.v3",
    "sourceCommit": args.source_commit,
}}
if outcome == "PERSISTED":
    if args.phase == "image":
        if control.get("RELEASE_LEGACY_IMAGE") == "1":
            evidence = {{"runtime_image_digest": digest}}
        else:
            evidence = {{"runtime_image_evidence": {{
                "account": args.account,
                "commitTag": f"commit-{{args.source_commit}}",
                "criticalFindings": 0,
                "highFindings": 0,
                "imageDigest": digest,
                "imageSizeBytes": 1,
                "imageUri": image_uri,
                "provenanceSha256": "2" * 64,
                "region": args.region,
                "repositoryName": "personal-operator/bridge",
                "sbomSha256": "1" * 64,
                "scanStatus": "COMPLETE",
                "schema": "personal-operator.runtime-image-evidence.v1",
                "signatureStatus": "SIGNED",
                "signingProfileArn": (
                    f"arn:aws:signer:{{args.region}}:{{args.account}}:/"
                    "signing-profiles/personal_operator_bridge"
                ),
                "sourceCommit": args.source_commit,
                "sourceTree": args.source_tree,
            }}}}
    elif args.phase == "runtime":
        evidence = {{"runtime_id": "Runtime-ABCDEFGHIJ", "runtime_version": "7"}}
    elif args.phase in {{"endpoint", "context"}}:
        if args.phase == "endpoint" and control.get("RELEASE_EMPTY_ENDPOINT") == "1":
            evidence = {{}}
        else:
            evidence = {{"runtime_context": context}}
            if args.phase == "context":
                payload = (json.dumps(context, separators=(",", ":"), sort_keys=True) + "\\n").encode()
                evidence["runtime_context_sha256"] = hashlib.sha256(payload).hexdigest()
    elif args.phase == "rollback":
        evidence = {{"rollback_reference": {rollback!r}}}
observation = {{
    "account": args.account,
    "evidence": evidence,
    "operationSha256": args.operation_sha256,
    "outcome": outcome,
    "phase": args.phase,
    "region": args.region,
    "schema": "personal-operator.phase-observation.v1",
    "sourceCommit": args.source_commit,
    "sourceTree": args.source_tree,
    "transactionId": args.transaction_id,
}}
print(json.dumps(observation, separators=(",", ":"), sort_keys=True))
""",
    )
    journal = tmp_path / "journal.json"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        "AWS_ACCESS_KEY_ID": "poison",
        "AWS_SECRET_ACCESS_KEY": "poison",
        "AWS_SESSION_TOKEN": "poison",
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp_path / "poison-token"),
        "AWS_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/poison",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9/poison",
    }
    fixture = {
        "repo": repository,
        "commit": commit,
        "tree": tree,
        "journal": journal,
        "driver": driver,
        "log": call_log,
        "rollback": rollback,
        "env": env,
    }
    if include_observation_config:
        config_path = Path(str(journal) + ".production-observation.json")
        config_path.write_bytes(_observation_config(fixture).to_bytes())
    wrapper = tmp_path / "staging-release-test-entrypoint.py"
    wrapper.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, {str(ROOT)!r})
import release_tools.cli as release_cli
from release_tools.cli import main
from release_tools.production_observation import ProductionObservationError

release_cli._trusted_aws_cli = lambda: Path({str(fake_bin / 'aws')!r})
CONTROL_PATH = Path(os.environ["PERSONAL_OPERATOR_TEST_CONTROL"])


class LiveAuthority:
    def observe_phase(self, phase, transaction):
        control = json.loads(
            CONTROL_PATH.read_text(encoding="utf-8")
        )
        if control.get("RELEASE_FAIL_OBSERVE_PHASE") == phase:
            raise ProductionObservationError(
                f"{{phase}} live observation authority is unavailable"
            )
        if control.get("RELEASE_OBSERVE_OUTCOME") == "ABSENT":
            return False, {{}}
        if phase in {{"foundation", "endpoint", "rollback"}}:
            return True, {{}}
        if phase == "image":
            return True, {{"runtime_image_digest": "sha256:" + "0" * 64}}
        if phase == "runtime":
            return True, {{
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "7",
            }}
        if phase == "context":
            return True, {{"runtime_context_sha256": "1" * 64}}
        if phase == "consumer-changesets":
            return True, {{"consumer_changesets_sha256": "2" * 64}}
        if phase == "consumers":
            return True, {{"consumer_application_sha256": "3" * 64}}
        if phase == "verify":
            return True, {{"verification_sha256": "4" * 64}}
        raise ProductionObservationError(f"unknown test phase {{phase}}")


raise SystemExit(main(composer_factory=lambda config: LiveAuthority()))
""",
        encoding="utf-8",
    )
    fixture["script"] = wrapper
    return fixture


def _run(
    fixture: dict[str, object],
    *arguments: str,
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    controls = {
        name: value
        for name, value in environment.items()
        if name.startswith("RELEASE_")
    }
    control_path = Path(str(fixture["journal"]) + ".driver-control.json")
    control_path.write_text(json.dumps(controls, sort_keys=True), encoding="utf-8")
    env = {
        **fixture["env"],
        "PERSONAL_OPERATOR_TEST_CONTROL": str(control_path),
        **{
            name: value
            for name, value in environment.items()
            if not name.startswith("RELEASE_")
        },
    }
    return subprocess.run(
        [
            sys.executable,
            str(fixture["script"]),
            "--root",
            str(fixture["repo"]),
            "--account",
            ACCOUNT,
            "--region",
            REGION,
            "--commit",
            str(fixture["commit"]),
            *arguments,
        ],
        cwd=fixture["repo"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _preflight(fixture: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--preflight",
    )


def _phase(
    fixture: dict[str, object],
    phase: str,
    *,
    confirmation: str | None = None,
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    transaction_id = f"release_{fixture['commit']}"
    operation_sha256 = _operation_sha256(fixture)
    args = [
        "--journal",
        str(fixture["journal"]),
        "--phase",
        phase,
        "--driver",
        str(fixture["driver"]),
        "--rollback-reference",
        str(fixture["rollback"]),
    ]
    if confirmation is not None:
        args.extend(["--confirm", confirmation])
    elif phase:
        args.extend(
            [
                "--confirm",
                f"mutate:{transaction_id}:{phase}:{operation_sha256}",
            ]
        )
    return _run(fixture, *args, **environment)


def test_help_exposes_only_the_explicit_release_modes() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    for option in (
        "--preflight",
        "--phase",
        "--resume",
        "--status",
        "--rollback",
    ):
        assert option in completed.stdout
    assert "agentcore deploy" not in completed.stdout.casefold()


def test_preflight_and_status_never_discover_aws_credentials(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    preflight = _preflight(fixture)
    status = _run(fixture, "--status", str(fixture["journal"]))

    assert preflight.returncode == 0, preflight.stderr
    assert status.returncode == 0, status.stderr
    assert not fixture["log"].exists()
    current = StagingTransactionV1.from_bytes(status.stdout.encode("utf-8"))
    assert current.state == "PREFLIGHTED"


def test_production_entrypoint_rejects_a_different_release_root(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--root",
            str(fixture["repo"]),
            "--account",
            ACCOUNT,
            "--commit",
            str(fixture["commit"]),
            "--journal",
            str(fixture["journal"]),
            "--preflight",
        ],
        cwd=fixture["repo"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "executing repository" in completed.stderr.casefold()
    assert not fixture["journal"].exists()


@pytest.mark.parametrize("mutation", ["dirty", "new-head"])
def test_phase_revalidates_exact_checkout_before_credentials(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    readme = fixture["repo"] / "README.md"
    readme.write_text("changed after preflight\n", encoding="utf-8")
    if mutation == "new-head":
        subprocess.run(
            ["git", "add", "README.md"], cwd=fixture["repo"], check=True
        )
        subprocess.run(
            ["git", "commit", "-qm", "change candidate"],
            cwd=fixture["repo"],
            check=True,
        )

    completed = _phase(fixture, "foundation")

    assert completed.returncode != 0
    assert "checkout" in completed.stderr.casefold()
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_phase_revalidates_checkout_after_driver_before_live_composition(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _write_executable(
        fixture["driver"],
        f"""#!{sys.executable}
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("mode", "phase", "journal", "transaction-id", "source-commit", "source-tree", "account", "region", "operation-sha256"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
Path({str(fixture["repo"] / "README.md")!r}).write_text("driver dirtied checkout\\n", encoding="utf-8")
print(json.dumps({{"dispatched": True}}, separators=(",", ":"), sort_keys=True))
""",
    )
    assert _preflight(fixture).returncode == 0

    completed = _phase(fixture, "foundation")

    assert completed.returncode != 0
    assert "checkout" in completed.stderr.casefold()
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "UNCERTAIN"
    assert current.last_stable_state == "PREFLIGHTED"


def test_mutation_requires_exact_confirmation_before_credentials_or_driver(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(
        fixture,
        "foundation",
        confirmation="mutate:wrong:foundation",
    )

    assert completed.returncode != 0
    assert "confirmation" in completed.stderr.casefold()
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_mutation_requires_exact_observation_config_before_write_ahead(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, include_observation_config=False)
    assert _preflight(fixture).returncode == 0

    completed = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--phase",
        "foundation",
        "--driver",
        str(fixture["driver"]),
        "--rollback-reference",
        str(fixture["rollback"]),
        "--confirm",
        "mutate:unreachable",
    )

    assert completed.returncode != 0
    assert "observation config" in completed.stderr.casefold()
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_mutation_rejects_a_symlinked_driver_before_write_ahead_or_credentials(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    link = tmp_path / "phase-driver-link"
    link.symlink_to(fixture["driver"])

    completed = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--phase",
        "foundation",
        "--driver",
        str(link),
        "--rollback-reference",
        str(fixture["rollback"]),
        "--confirm",
        (
            f"mutate:release_{fixture['commit']}:foundation:"
            f"{_operation_sha256(fixture)}"
        ),
    )

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.casefold()
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_driver_self_modification_stays_uncertain_under_recorded_digest(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    _write_executable(
        fixture["driver"],
        f"""#!{sys.executable}
import json
import pathlib
import sys

mode = sys.argv[sys.argv.index("--mode") + 1]
if mode == "mutate":
    path = pathlib.Path(sys.argv[0])
    path.write_text("#!{sys.executable}\\nprint('changed')\\n", encoding="utf-8")
    path.chmod(0o700)
    print(json.dumps({{"dispatched": True}}, separators=(",", ":"), sort_keys=True))
else:
    print("changed")
""",
    )
    assert _preflight(fixture).returncode == 0

    completed = _phase(fixture, "foundation")

    assert completed.returncode != 0
    assert "operation bytes changed" in completed.stderr.casefold()
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "UNCERTAIN"
    assert current.uncertain_operation_sha256 == _operation_sha256(fixture)


def test_driver_cannot_import_mutable_operator_helper_from_pythonpath(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    helper_root = tmp_path / "unreviewed-helper"
    helper_root.mkdir()
    marker = tmp_path / "helper-imported"
    (helper_root / "phase_helper.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    _write_executable(
        fixture["driver"],
        f"""#!{sys.executable}
import argparse
import json
import phase_helper

parser = argparse.ArgumentParser()
for name in ("mode", "phase", "journal", "transaction-id", "source-commit", "source-tree", "account", "region", "operation-sha256"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
if args.mode == "mutate":
    value = {{"dispatched": True}}
else:
    value = {{
        "account": args.account,
        "evidence": {{}},
        "operationSha256": args.operation_sha256,
        "outcome": "PERSISTED",
        "phase": args.phase,
        "region": args.region,
        "schema": "personal-operator.phase-observation.v1",
        "sourceCommit": args.source_commit,
        "sourceTree": args.source_tree,
        "transactionId": getattr(args, "transaction_id"),
    }}
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
""",
    )
    assert _preflight(fixture).returncode == 0

    completed = _phase(
        fixture,
        "foundation",
        PYTHONPATH=str(helper_root),
    )

    assert completed.returncode != 0
    assert not marker.exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "UNCERTAIN"


def test_driver_environment_is_account_pinned_and_code_loading_closed(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    environment_log = tmp_path / "driver-environment.json"
    _write_executable(
        fixture["driver"],
        f"""#!{sys.executable}
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("mode", "phase", "journal", "transaction-id", "source-commit", "source-tree", "account", "region", "operation-sha256"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
Path({str(environment_log)!r}).write_text(
    json.dumps(dict(os.environ), sort_keys=True), encoding="utf-8"
)
if args.mode == "mutate":
    value = {{"dispatched": True}}
else:
    value = {{
        "account": args.account,
        "evidence": {{}},
        "operationSha256": args.operation_sha256,
        "outcome": "PERSISTED",
        "phase": args.phase,
        "region": args.region,
        "schema": "personal-operator.phase-observation.v1",
        "sourceCommit": args.source_commit,
        "sourceTree": args.source_tree,
        "transactionId": getattr(args, "transaction_id"),
    }}
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
""",
    )
    assert _preflight(fixture).returncode == 0

    completed = _phase(
        fixture,
        "foundation",
        CDK_DEFAULT_ACCOUNT="999999999999",
        AWS_ENDPOINT_URL="http://127.0.0.1:9",
        PYTHONPATH="/tmp/unreviewed",
        HTTP_PROXY="http://127.0.0.1:8",
    )

    assert completed.returncode == 0, completed.stderr
    observed = json.loads(environment_log.read_text(encoding="utf-8"))
    assert observed["CDK_DEFAULT_ACCOUNT"] == ACCOUNT
    assert observed["AWS_REGION"] == REGION
    assert observed["AWS_DEFAULT_REGION"] == REGION
    assert observed["CDK_DEFAULT_REGION"] == REGION
    for name in (
        "AWS_ENDPOINT_URL",
        "PYTHONPATH",
        "HTTP_PROXY",
    ):
        assert name not in observed


def test_confirmed_phase_discovers_exact_account_immediately_before_driver(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(fixture, "foundation")

    assert completed.returncode == 0, completed.stderr
    assert fixture["log"].read_text(encoding="utf-8").splitlines() == [
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
        "driver mutate <foundation> region=<eu-west-1>/<eu-west-1>/<eu-west-1>",
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
    ]
    assert TransactionJournal.load(fixture["journal"]).current.state == (
        "FOUNDATION_READY"
    )


def test_forged_driver_observation_cannot_advance_the_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    journal = TransactionJournal.load(fixture["journal"])
    config = _observation_config(fixture)
    operation_sha256 = release_cli._reviewed_operation_sha256(
        fixture["driver"].read_bytes(),
        config,
    )
    args = type(
        "Args",
        (),
        {
            "driver": fixture["driver"],
            "confirm": (
                f"mutate:{journal.current.transaction_id}:foundation:"
                f"{operation_sha256}"
            ),
            "rollback_reference": fixture["rollback"],
        },
    )()

    class LiveAuthority:
        def observe_phase(self, phase, transaction):
            assert phase == "foundation"
            assert transaction.state == "UNCERTAIN"
            return False, {}

    monkeypatch.setattr(release_cli, "_discover_account", lambda *args, **kwargs: None)

    current = release_cli._run_phase(
        journal,
        "foundation",
        args,
        observation_config=config,
        composer_factory=lambda config: LiveAuthority(),
    )

    assert current.state == "PREFLIGHTED"
    assert fixture["log"].read_text(encoding="utf-8").splitlines() == [
        "driver mutate <foundation> region=<eu-west-1>/<eu-west-1>/<eu-west-1>"
    ]


def test_uncertain_operation_digest_binds_the_observation_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    journal = TransactionJournal.load(fixture["journal"])
    config = _observation_config(fixture)
    operation_sha256 = release_cli._reviewed_operation_sha256(
        fixture["driver"].read_bytes(),
        config,
    )
    changed = replace(config, builder_inputs=("sha256:" + "e" * 64,))
    changed_sha256 = release_cli._reviewed_operation_sha256(
        fixture["driver"].read_bytes(),
        changed,
    )
    changed_stack = replace(
        config,
        foundation_stack_template_parameter_digests=tuple(
            (
                name,
                "9" * 64 if name == "OpenClawVpc" else digest,
            )
            for name, digest in config.foundation_stack_template_parameter_digests
        ),
    )
    changed_stack_sha256 = release_cli._reviewed_operation_sha256(
        fixture["driver"].read_bytes(),
        changed_stack,
    )
    args = type(
        "Args",
        (),
        {
            "driver": fixture["driver"],
            "confirm": (
                f"mutate:{journal.current.transaction_id}:foundation:"
                f"{operation_sha256}"
            ),
            "rollback_reference": fixture["rollback"],
        },
    )()

    class UnavailableAuthority:
        def observe_phase(self, phase, transaction):
            raise RuntimeError("live authority unavailable")

    monkeypatch.setattr(release_cli, "_discover_account", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        release_cli._run_phase(
            journal,
            "foundation",
            args,
            observation_config=config,
            composer_factory=lambda config: UnavailableAuthority(),
        )

    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "UNCERTAIN"
    assert current.uncertain_operation_sha256 == operation_sha256
    assert changed_sha256 != operation_sha256
    assert changed_stack_sha256 != operation_sha256


def test_phase_revalidates_region_before_write_ahead_or_credentials(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(fixture, "foundation", AWS_REGION="us-east-1")

    assert completed.returncode != 0
    assert "AWS_REGION" in completed.stderr
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_mutation_requires_typed_ack_and_authoritative_observation(
    tmp_path: Path,
) -> None:
    bad_ack = _fixture(tmp_path / "ack")
    assert _preflight(bad_ack).returncode == 0

    rejected = _phase(
        bad_ack,
        "foundation",
        RELEASE_BAD_ACK_PHASE="foundation",
    )

    assert rejected.returncode != 0
    assert "acknowledgement" in rejected.stderr
    assert TransactionJournal.load(bad_ack["journal"]).current.state == "UNCERTAIN"

    no_observation = _fixture(tmp_path / "observe")
    assert _preflight(no_observation).returncode == 0
    ambiguous = _phase(
        no_observation,
        "foundation",
        RELEASE_FAIL_OBSERVE_PHASE="foundation",
    )

    assert ambiguous.returncode != 0
    assert "live observation authority" in ambiguous.stderr
    assert TransactionJournal.load(no_observation["journal"]).current.state == (
        "UNCERTAIN"
    )


def test_post_dispatch_failure_stays_uncertain_and_blocks_later_phases(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    fixture["log"].unlink()

    failed = _phase(fixture, "image", RELEASE_FAIL_PHASE="image")
    after_failure = TransactionJournal.load(fixture["journal"]).current
    later = _phase(fixture, "runtime")
    resume = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
    )

    assert failed.returncode != 0
    assert after_failure.state == "UNCERTAIN"
    assert after_failure.last_stable_state == "FOUNDATION_READY"
    assert after_failure.uncertain_phase == "IMAGE_PUBLISHED"
    assert later.returncode != 0
    assert resume.returncode != 0
    assert "reconcile" in resume.stderr.casefold()
    assert fixture["log"].read_text(encoding="utf-8").splitlines().count(
        "driver mutate <image> region=<eu-west-1>/<eu-west-1>/<eu-west-1>"
    ) == 1
    assert "<runtime>" not in fixture["log"].read_text(encoding="utf-8")


def test_image_driver_observation_is_ignored_in_favor_of_live_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0

    completed = _phase(fixture, "image", RELEASE_LEGACY_IMAGE="1")

    assert completed.returncode == 0, completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "IMAGE_PUBLISHED"
    assert current.runtime_image_digest == "sha256:" + "0" * 64
    assert "driver observe" not in fixture["log"].read_text(encoding="utf-8")


def test_endpoint_driver_observation_is_ignored_in_favor_of_live_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image").returncode == 0
    assert _phase(fixture, "runtime").returncode == 0

    completed = _phase(fixture, "endpoint", RELEASE_EMPTY_ENDPOINT="1")

    assert completed.returncode == 0, completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "ENDPOINT_READY"
    assert "driver observe" not in fixture["log"].read_text(encoding="utf-8")


def test_explicit_absent_reconciliation_allows_safe_resume(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image", RELEASE_FAIL_PHASE="image").returncode != 0

    reconciled = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        (
            f"reconcile:release_{fixture['commit']}:image:"
            f"{_operation_sha256(fixture)}"
        ),
        RELEASE_OBSERVE_OUTCOME="ABSENT",
    )
    resumed = _phase(fixture, "image")

    assert reconciled.returncode == 0, reconciled.stderr
    assert resumed.returncode == 0, resumed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "IMAGE_PUBLISHED"
    assert current.runtime_image_digest == "sha256:" + "0" * 64


def test_reconciliation_rejects_operator_outcome_and_changed_driver(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image", RELEASE_FAIL_PHASE="image").returncode != 0
    before_calls = fixture["log"].read_text(encoding="utf-8")

    operator_claim = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "persisted",
    )
    fixture["driver"].write_text(
        fixture["driver"].read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    fixture["driver"].chmod(0o755)
    changed_digest = _operation_sha256(fixture)
    replaced = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        (
            f"reconcile:release_{fixture['commit']}:image:"
            f"{changed_digest}"
        ),
    )

    assert operator_claim.returncode != 0
    assert replaced.returncode != 0
    assert "digest differs" in replaced.stderr
    assert fixture["log"].read_text(encoding="utf-8") == before_calls
    assert TransactionJournal.load(fixture["journal"]).current.state == "UNCERTAIN"


def test_reconciliation_revalidates_region_before_live_observation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image", RELEASE_FAIL_PHASE="image").returncode != 0
    before_calls = fixture["log"].read_text(encoding="utf-8")

    completed = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        (
            f"reconcile:release_{fixture['commit']}:image:"
            f"{_operation_sha256(fixture)}"
        ),
        AWS_DEFAULT_REGION="us-east-1",
    )

    assert completed.returncode != 0
    assert "AWS_DEFAULT_REGION" in completed.stderr
    assert fixture["log"].read_text(encoding="utf-8") == before_calls
    assert TransactionJournal.load(fixture["journal"]).current.state == "UNCERTAIN"


def test_verified_rollback_is_write_ahead_and_never_exposes_endpoint_retarget(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    verified = StagingTransactionV1.from_mapping(
        {
            "schema": StagingTransactionV1.SCHEMA,
            "transactionId": f"release_{fixture['commit']}",
            "sourceCommit": fixture["commit"],
            "sourceTree": fixture["tree"],
            "account": ACCOUNT,
            "region": REGION,
            "state": "VERIFIED",
            "lastStableState": "VERIFIED",
            "revision": 9,
            "runtimeImageDigest": "sha256:" + "0" * 64,
            "runtimeId": "Runtime-ABCDEFGHIJ",
            "runtimeVersion": "7",
            "runtimeEndpointName": f"release_{fixture['commit']}",
            "runtimeContextSha256": "1" * 64,
            "consumerChangesetsSha256": "2" * 64,
            "consumerApplicationSha256": "3" * 64,
            "verificationSha256": "4" * 64,
            "rollbackReference": fixture["rollback"],
            "uncertainPhase": "",
            "uncertainOperationSha256": "",
        }
    )
    write_new_contract(fixture["journal"], verified)
    transaction_id = f"release_{fixture['commit']}"
    operation_sha256 = _operation_sha256(fixture)

    poisoned = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--rollback",
        transaction_id,
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"rollback:{transaction_id}:{operation_sha256}",
        CDK_DEFAULT_REGION="us-east-1",
    )
    assert poisoned.returncode != 0
    assert "CDK_DEFAULT_REGION" in poisoned.stderr
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "VERIFIED"

    completed = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--rollback",
        transaction_id,
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"rollback:{transaction_id}:{operation_sha256}",
    )

    assert completed.returncode == 0, completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "ROLLED_BACK"
    assert current.runtime_endpoint_name == f"release_{fixture['commit']}"
    assert fixture["log"].read_text(encoding="utf-8").splitlines()[-3:] == [
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
        "driver mutate <rollback> region=<eu-west-1>/<eu-west-1>/<eu-west-1>",
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
    ]


def test_status_rejects_noncanonical_journal_without_aws_access(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["journal"].write_bytes(canonical_json_bytes({"schema": "wrong"}))

    completed = _run(fixture, "--status", str(fixture["journal"]))

    assert completed.returncode != 0
    assert not fixture["log"].exists()
