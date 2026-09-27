"""Synth-time test for the per-user state bucket lifecycle (prod decision D5).

The bucket is versioned and the bridge re-saves state often, so without a
noncurrent-version rule it grows without bound (prod: ~915 GB / 7.6 M versions).
The rule must expire only NONCURRENT versions, keep the newest few per key for
rollback, keep the pre-existing current-object rule, and never expire delete
markers or suspend versioning.

    pytest tests/test_state_bucket_lifecycle_synth.py -v
"""

import pytest

from tests.test_guardrail_wiring_synth import _synth_agentcore


@pytest.fixture(scope="module")
def bucket_props():
    template, _ = _synth_agentcore(enable_guardrails=False)
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 1, buckets.keys()
    return next(iter(buckets.values()))["Properties"]


def _rules_by_id(props):
    return {r["Id"]: r for r in props["LifecycleConfiguration"]["Rules"]}


def test_versioning_stays_enabled(bucket_props):
    assert bucket_props["VersioningConfiguration"] == {"Status": "Enabled"}


def test_exactly_three_rules(bucket_props):
    assert sorted(_rules_by_id(bucket_props)) == [
        "abort-incomplete-multipart-uploads",
        "expire-noncurrent-versions",
        "expire-old-user-files",
    ]


def test_existing_current_object_rule_preserved(bucket_props):
    rule = _rules_by_id(bucket_props)["expire-old-user-files"]
    assert rule["Status"] == "Enabled"
    assert rule["ExpirationInDays"] == 365


def test_noncurrent_rule_keeps_newest_three_for_30_days(bucket_props):
    rule = _rules_by_id(bucket_props)["expire-noncurrent-versions"]
    assert rule["Status"] == "Enabled"
    assert rule["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": 30,
        "NewerNoncurrentVersions": 3,
    }
    # Must not touch current objects or delete markers.
    for forbidden in ("ExpirationInDays", "ExpirationDate", "ExpiredObjectDeleteMarker"):
        assert forbidden not in rule, forbidden


def test_abort_incomplete_multipart_after_7_days(bucket_props):
    rule = _rules_by_id(bucket_props)["abort-incomplete-multipart-uploads"]
    assert rule["Status"] == "Enabled"
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 7}
    for forbidden in ("ExpirationInDays", "ExpiredObjectDeleteMarker", "NoncurrentVersionExpiration"):
        assert forbidden not in rule, forbidden


def test_no_rule_expires_delete_markers(bucket_props):
    for rule in bucket_props["LifecycleConfiguration"]["Rules"]:
        assert "ExpiredObjectDeleteMarker" not in rule
