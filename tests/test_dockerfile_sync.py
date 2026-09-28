"""The tracked Starter Toolkit Dockerfile must match bridge/Dockerfile.

scripts/deploy.sh copies bridge/Dockerfile over
.bedrock_agentcore/openclaw_agent/Dockerfile before every build, so deployed
images always use bridge/Dockerfile. The tracked copy is still what readers
and hand builds see, so a missing COPY line there is misleading. This test
fails when the COPY instructions in the two files drift apart.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DOCKERFILE = REPO_ROOT / "bridge" / "Dockerfile"
TOOLKIT_DOCKERFILE = REPO_ROOT / ".bedrock_agentcore" / "openclaw_agent" / "Dockerfile"


def _copy_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip().upper().startswith("COPY ")
    ]


def test_toolkit_dockerfile_copy_lines_match_bridge():
    bridge = _copy_lines(BRIDGE_DOCKERFILE)
    toolkit = _copy_lines(TOOLKIT_DOCKERFILE)
    missing = [line for line in bridge if line not in toolkit]
    extra = [line for line in toolkit if line not in bridge]
    assert bridge == toolkit, (
        f"{TOOLKIT_DOCKERFILE.relative_to(REPO_ROOT)} is out of sync with "
        f"{BRIDGE_DOCKERFILE.relative_to(REPO_ROOT)}; deploy.sh overwrites it "
        "with bridge/Dockerfile, so copy that file over it.\n"
        f"missing: {missing}\nextra: {extra}"
    )
