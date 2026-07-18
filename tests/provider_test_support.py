"""Stable references to the real provider SDK types used by hermetic tests.

Some retained router tests replace ``sys.modules`` entries while they are
collected.  Import this module from the root pytest configuration before test
collection so later tests can still construct real botocore errors and fence
the real botocore request boundary.
"""

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError


__all__ = ["BaseClient", "ClientError", "boto3"]
