"""The bridge image must not ship undici below the GHSA-xjj6-f33q-929c fix.

The deployed OpenClaw gateway can load undici from three places: Node's bundled
global fetch, the globally installed ``openclaw`` package, and the globally
installed ``clawhub`` package. All three were vulnerable on the pinned
base image / package versions. This PR pins the base image to Node 24.22.1
(undici 7.30.1), bumps clawhub to a release whose undici is fixed, and overrides
openclaw's bundled undici to 8.12.2.

These are static checks over ``bridge/Dockerfile`` and the bridge sources: they
need neither Docker, an arm64 builder, nor the CDK toolchain, so they run in the
same offline suite as the other bridge tests. The running image is additionally
gated at build time by ``bridge/undici-floor-check.js``.
"""

import re
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"
DOCKERFILE = BRIDGE / "Dockerfile"

# node:24.22.1-slim, linux/arm64 single-arch child manifest (undici 7.30.1).
NODE_TAG = "24.22.1-slim"
NODE_DIGEST = "sha256:932a44adcdb8f5dfbf00f00eb38da8d22b450b2ad258b6d1b2319beca823bfbd"
EXPECTED_NODE = "24.22.1"

# The runtime guards that assert the exact pinned Node version. They must track
# the base image or the gateway refuses to start on the new image.
VERSION_PINNED_FILES = (
    "gateway-invariant-prewarm.mjs",
    "live-gateway-seed-owner.mjs",
    "proxy-sdk-snapshot.js",
    "native-owner.test.js",
)


def _dockerfile():
    return DOCKERFILE.read_text()


def _from_node_lines():
    return [line for line in _dockerfile().splitlines()
            if line.startswith("FROM") and "node:" in line]


def test_both_base_images_are_pinned_to_the_fixed_node():
    froms = _from_node_lines()
    assert len(froms) == 2, f"expected two node FROM lines, got {froms}"
    for line in froms:
        assert f"node:{NODE_TAG}@{NODE_DIGEST}" in line, line
    # No stale reference to the vulnerable base image anywhere in the Dockerfile.
    assert "24.21.0" not in _dockerfile()


def test_clawhub_is_bumped_to_a_fixed_release():
    text = _dockerfile()
    assert "clawhub@0.25.1" in text
    assert "clawhub@0.23.3" not in text


def test_openclaw_undici_is_overridden_to_the_fixed_version():
    assert "undici@8.12.2" in _dockerfile()


def test_the_build_runs_the_undici_floor_check():
    assert "undici-floor-check.js" in _dockerfile()
    check = BRIDGE / "undici-floor-check.js"
    assert check.exists(), "bridge/undici-floor-check.js is missing"
    body = check.read_text()
    # The three GHSA-xjj6-f33q-929c floors the gate enforces.
    for floor in ('"7.30.1"', '"8.12.2"'):
        assert floor in body, f"floor {floor} not enforced by undici-floor-check.js"


def test_runtime_version_guards_track_the_base_image():
    want = f"v{EXPECTED_NODE}"
    for name in VERSION_PINNED_FILES:
        text = (BRIDGE / name).read_text()
        assert want in text, f"{name} does not pin {want}"
        assert "v24.21.0" not in text, f"{name} still pins the old v24.21.0"
