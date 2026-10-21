#!/usr/bin/env python3
"""Environment-suffix naming for the deployment scripts.

Mirrors stacks/__init__.py (environment_suffix / DeploymentNamer) without
importing aws_cdk, so it also works in the SDK-controller venv. The suffix
comes from the OPENCLAW_ENV_SUFFIX environment variable (deploy.sh exports
the resolved value). Empty, the default, leaves every name unchanged.
"""

import os
import re

_SUFFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_LEN = 12


def env_suffix(environ=None) -> str:
    raw = (os.environ if environ is None else environ).get("OPENCLAW_ENV_SUFFIX", "")
    suffix = str(raw).strip().lower().strip("-")
    if suffix and (not _SUFFIX_RE.fullmatch(suffix) or len(suffix) > MAX_LEN):
        raise ValueError(
            "OPENCLAW_ENV_SUFFIX must be at most 12 lowercase letters, digits and single hyphens"
        )
    return suffix


def with_suffix(base: str, environ=None, separator: str = "-") -> str:
    suffix = env_suffix(environ)
    if not suffix:
        return base
    if separator == "_":
        suffix = suffix.replace("-", "_")
    return f"{base}{separator}{suffix}"
