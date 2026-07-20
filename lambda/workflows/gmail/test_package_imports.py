import importlib
from pathlib import Path
import sys


def test_workflow_imports_as_lambda_asset_package_without_test_aliases():
    lambda_root = str(Path(__file__).resolve().parents[2])
    sys.path.insert(0, lambda_root)
    try:
        sys.modules.pop("workflows.index", None)
        module = importlib.import_module("workflows.index")
    finally:
        sys.path.remove(lambda_root)

    assert module.GmailPilotWorkflow.__module__ == "workflows.index"
    assert module.DraftRevision.__module__ == "workflows.gmail.models"
