"""Frozen Personal Operator capability contracts and catalog compiler."""

from .catalog import compile_catalog
from .contracts import *  # noqa: F401,F403 - stable contract surface
from .contracts import __all__ as _contract_exports

__all__ = [*_contract_exports, "compile_catalog"]
