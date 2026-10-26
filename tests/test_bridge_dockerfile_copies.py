"""Every local module a bridge file loads must be copied into the same image stage.

The runtime image only contains the files bridge/Dockerfile copies. A new
``require("./x")`` (or ``import ... from "./x.mjs"``) without a matching COPY
passes every unit test, because the tests run from the repo checkout, and only
fails when the container starts ("Cannot find module './x'"). This test
rebuilds each stage's file list from the Dockerfile COPY instructions and
resolves every relative module reference of every copied JS/MJS file against it.
"""

import re
import shlex
import sys
from pathlib import Path, PurePosixPath

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
DOCKERFILE = BRIDGE / "Dockerfile"

# Relative module specifiers: require("./x"), createRequire(...)("./x"),
# import ... from "./x", import("./x"), new URL("./x", import.meta.url).
_SPECIFIER_PATTERNS = [
    re.compile(r"""\brequire\(\s*["'](\.{1,2}/[^"']+)["']\s*\)"""),
    re.compile(r"""\)\(\s*["'](\.{1,2}/[^"']+)["']\s*\)"""),
    re.compile(r"""\bfrom\s+["'](\.{1,2}/[^"']+)["']"""),
    re.compile(r"""\bimport\(\s*["'](\.{1,2}/[^"']+)["']"""),
    re.compile(r"""\bnew URL\(\s*["'](\.{1,2}/[^"']+)["']\s*,\s*import\.meta\.url"""),
    # `${__dirname}/x.mjs` as a spawn argument
    re.compile(r"""\$\{__dirname\}/([^`"'\s]+)"""),
]
# Absolute in-image paths of application files, e.g. "/app/agentcore-proxy.js".
_ABSOLUTE_APP_FILE = re.compile(r"""["'`](/app/[A-Za-z0-9._/-]+\.(?:js|mjs|json))["'`]""")
_COMMENT_LINE = re.compile(r"^\s*(//|\*|/\*)")


def _instructions(text):
    """Yield Dockerfile instructions with line continuations joined."""
    current = ""
    for line in text.splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.rstrip().endswith("\\"):
            current += line.rstrip()[:-1] + " "
            continue
        current += line
        yield current.strip()
        current = ""
    if current:
        yield current.strip()


def _stages():
    """Return one {in-image path: repo path} map per build stage."""
    stages = []
    for instruction in _instructions(DOCKERFILE.read_text()):
        keyword = instruction.split(None, 1)[0].upper()
        if keyword == "FROM":
            stages.append({})
            continue
        if keyword not in ("COPY", "ADD"):
            continue
        words = shlex.split(instruction)[1:]
        if any(w.startswith("--from") for w in words):
            continue  # copies a previous stage's output, not repo files
        args = [w for w in words if not w.startswith("--")]
        *sources, dest = args
        files = stages[-1]
        for src in sources:
            repo_src = BRIDGE / src
            assert repo_src.exists(), f"Dockerfile COPY source missing from bridge/: {src}"
            if repo_src.is_dir():
                for path in repo_src.rglob("*"):
                    if path.is_file():
                        rel = path.relative_to(repo_src).as_posix()
                        files[str(PurePosixPath(dest) / rel)] = path
            elif dest.endswith("/") or len(sources) > 1:
                files[str(PurePosixPath(dest) / repo_src.name)] = repo_src
            else:
                files[dest] = repo_src
    return stages


def _resolve(image_files, base_dir, specifier):
    target = PurePosixPath(base_dir) / specifier
    # Normalise ./ and ../ without touching the filesystem.
    parts = []
    for part in target.parts:
        if part == "..":
            parts.pop()
        elif part != ".":
            parts.append(part)
    target = str(PurePosixPath(*parts))
    candidates = [target, target + ".js", target + ".json", target + "/index.js"]
    return next((c for c in candidates if c in image_files), None)


def _references(source_text):
    for line in source_text.splitlines():
        if _COMMENT_LINE.match(line):
            continue
        for pattern in _SPECIFIER_PATTERNS:
            for match in pattern.finditer(line):
                yield "relative", match.group(1)
        for match in _ABSOLUTE_APP_FILE.finditer(line):
            yield "absolute", match.group(1)


def _missing():
    missing = []
    for files in _stages():
        for image_path, repo_path in files.items():
            if repo_path.suffix not in (".js", ".mjs") or ".test." in repo_path.name:
                continue
            base_dir = str(PurePosixPath(image_path).parent)
            for kind, ref in _references(repo_path.read_text()):
                if kind == "absolute":
                    found = ref if ref in files else None
                else:
                    found = _resolve(files, base_dir, ref)
                if found is None:
                    missing.append(f"{image_path} ({repo_path.relative_to(BRIDGE)}) -> {ref}")
    return sorted(set(missing))


def test_every_local_module_is_copied_into_the_image():
    assert _missing() == []


def test_entrypoint_targets_are_copied():
    runtime = _stages()[-1]
    entrypoint = (BRIDGE / "entrypoint.sh").read_text()
    targets = set(re.findall(r"(/app/[A-Za-z0-9._-]+\.(?:js|mjs))", entrypoint))
    assert targets, "entrypoint.sh no longer names any /app script"
    assert sorted(t for t in targets if t not in runtime) == []


def test_contract_server_dependencies_are_in_the_runtime_stage():
    # Guards the parser itself: the runtime stage must carry the contract
    # server and the modules it is known to load.
    runtime = _stages()[-1]
    for name in ("agentcore-contract.js", "agentcore-proxy.js", "channel-authz.js",
                 "cognito-token.js", "live-gateway-seed-owner.mjs"):
        assert f"/app/{name}" in runtime


def test_a_missing_copy_is_reported(monkeypatch):
    # Drop one COPY line and confirm the check names the dangling require.
    original = DOCKERFILE.read_text()
    trimmed = original.replace("COPY channel-authz.js /app/channel-authz.js\n", "")
    assert trimmed != original

    class _Fake:
        def read_text(self):
            return trimmed

    monkeypatch.setattr(sys.modules[__name__], "DOCKERFILE", _Fake())
    assert any(m.endswith("-> ./channel-authz") for m in _missing())
