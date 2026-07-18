"""Release-specific AgentCore endpoint identity contracts."""

from __future__ import annotations

import pytest

from runtime_state import canonical_runtime_qualifier


SOURCE_COMMIT = "a" * 40
RELEASE_ENDPOINT = f"release_{SOURCE_COMMIT}"


def test_runtime_qualifier_is_the_exact_release_endpoint() -> None:
    assert canonical_runtime_qualifier(RELEASE_ENDPOINT) == RELEASE_ENDPOINT


@pytest.mark.parametrize(
    "qualifier",
    [
        "DEFAULT",
        "release_latest",
        "release_" + "a" * 39,
        "release_" + "A" * 40,
        "release_" + "a" * 39 + "g",
    ],
)
def test_runtime_qualifier_rejects_mutable_or_noncanonical_endpoints(
    qualifier: str,
) -> None:
    with pytest.raises(ValueError, match="release endpoint"):
        canonical_runtime_qualifier(qualifier)
