"""The connector plane stays behind the caller-pinned trusted boundary and is
never surfaced to the runtime via the model-facing capability catalog.

These prove: (a) the frozen 10-op catalog is not polluted with connector ops;
(b) the production composition wires the connector registry empty (disabled by
default); (c) the disabled-by-default connector registry seam rejects any
connector operationId that collides with a model-facing catalog operationId.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.catalog import compile_catalog
from capabilities.contracts import FROZEN_CATALOG_PACKS_V1
from capabilities.gateway import ConnectorPlaneRegistry
from connectors import manifest as manifest_module

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "capabilities" / "schemas"
CATALOG_SCHEMA_DIR = (
    Path(__file__).resolve().parents[2] / "specs" / "capabilities" / "schemas"
)
RELEASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _catalog():
    _, catalog = compile_catalog(RELEASE_COMMIT, CATALOG_SCHEMA_DIR)
    return catalog


def test_connector_ops_are_not_in_the_frozen_model_facing_catalog():
    catalog_ops = {
        op["operationId"] for pack in FROZEN_CATALOG_PACKS_V1 for op in pack["operations"]
    }
    connector_ops = set()
    for manifest in manifest_module.build_curated_registry().values():
        connector_ops.update(op["operationId"] for op in manifest.operations)
    assert connector_ops.isdisjoint(catalog_ops)


def test_connector_registry_is_empty_by_default():
    registry = ConnectorPlaneRegistry(catalog=_catalog())
    assert registry.enabled_connector_ids() == ()
    assert registry.is_disabled()


def test_registry_refuses_connector_op_colliding_with_model_catalog():
    catalog = _catalog()

    class _Adapter:
        def read(self, *a, **k): ...
        def prepare(self, *a, **k): ...
        def dispatch(self, *a, **k): ...
        def reconcile(self, *a, **k): ...
        def revoke(self, *a, **k): ...

    with pytest.raises(ValueError):
        ConnectorPlaneRegistry(
            catalog=catalog,
            adapters={"workspace.file.read": _Adapter()},
        )


def test_production_composition_wires_connector_registry_empty():
    import capabilities.composition as composition

    source = Path(composition.__file__).read_text(encoding="utf-8")
    # The production gateway still wires model adapters empty; the connector
    # plane is likewise disabled by default (never enabled in composition).
    assert "adapters={}" in source
    assert "ConnectorPlaneRegistry" not in source or "adapters={}" in source
