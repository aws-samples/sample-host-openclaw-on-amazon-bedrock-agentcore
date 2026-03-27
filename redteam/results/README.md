# Red Team Results

This directory stores result snapshots from promptfoo red team runs.

## Saving a Snapshot

After a run, copy the results for archival:

```bash
cp .promptfoo/output/latest.json results/$(date +%Y%m%d)-baseline.json
cp .promptfoo/output/latest.json results/$(date +%Y%m%d)-hardened.json
```

## Interpreting Results

- Compare baseline vs hardened pass rates
- Check OWASP LLM Top 10 categorization
- Review individual failures for false positives
