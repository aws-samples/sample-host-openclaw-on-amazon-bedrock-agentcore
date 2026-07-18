"""Repository-wide pytest bootstrap for stable provider test references."""

# Import before collection-time legacy router stubs replace SDK modules.
from tests import provider_test_support as _provider_test_support


assert _provider_test_support.ClientError.__module__ == "botocore.exceptions"
