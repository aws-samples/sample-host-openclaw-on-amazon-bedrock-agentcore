"""Hermetic whole-app synth for the synth tests (no AWS calls).

``app.py`` runs in a child process because the jsii runtime reads
``CDK_OUTDIR``/``CDK_CONTEXT_JSON`` only at start. Results are cached per
context, so several test modules can share one default synth.
"""

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "123456789012"
REGION = "us-west-2"


def synth_app(**context_overrides):
    """Return {stack name: template} for app.py with cdk.json context plus overrides."""
    return _synth(json.dumps(context_overrides, sort_keys=True))


@functools.lru_cache(maxsize=None)
def _synth(overrides_json):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update(account=ACCOUNT, region=REGION,
                   availability_zones=[f"{REGION}a", f"{REGION}b"])
    context.update(json.loads(overrides_json))
    outdir = tempfile.mkdtemp(prefix="openclaw-synth-")
    env = dict(os.environ, CDK_CONTEXT_JSON=json.dumps(context), CDK_OUTDIR=outdir)
    env.pop("OPENCLAW_ENV_SUFFIX", None)
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=900)
    assert result.returncode == 0, result.stderr[-4000:]
    templates = {p.name.removesuffix(".template.json"): json.loads(p.read_text())
                 for p in Path(outdir).glob("*.template.json")}
    assert templates, "app.py synthesized no stacks"
    return templates


def resources(templates, res_type):
    """Yield (stack, logical id, resource) for every resource of `res_type`."""
    for stack, template in templates.items():
        for logical_id, res in template.get("Resources", {}).items():
            if res["Type"] == res_type:
                yield stack, logical_id, res


def secret_arns_by_export(templates, suffix="AbCdEf"):
    """Map each exported secret Ref to the ARN Secrets Manager would give it.

    Secrets Manager appends '-' and six random characters to a secret's name.
    """
    names = {lid: res["Properties"]["Name"]
             for _, lid, res in resources(templates, "AWS::SecretsManager::Secret")
             if isinstance(res["Properties"].get("Name"), str)}
    exports = {}
    for stack, template in templates.items():
        for output in template.get("Outputs", {}).values():
            ref = output.get("Value", {}).get("Ref") if isinstance(output.get("Value"), dict) else None
            export = output.get("Export", {}).get("Name")
            if ref in names and export:
                exports[export] = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{names[ref]}-{suffix}"
    return exports


def resolve(value, imports):
    """Evaluate the CloudFormation intrinsics CDK emits for cross-stack secret names."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        (fn, arg), = value.items()
        if fn == "Fn::ImportValue":
            return imports[resolve(arg, imports)]
        if fn == "Fn::Join":
            sep, parts = arg
            return sep.join(resolve(p, imports) for p in parts)
        if fn == "Fn::Split":
            sep, text = arg
            return resolve(text, imports).split(sep)
        if fn == "Fn::Select":
            index, items = arg
            items = resolve(items, imports) if not isinstance(items, list) else items
            return resolve(items[int(index)], imports)
    raise ValueError(f"cannot resolve {value!r}")


def iam_pattern_matches(pattern, arn):
    """IAM ARN wildcard match: '*' is any run of characters, '?' exactly one."""
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, arn) is not None
