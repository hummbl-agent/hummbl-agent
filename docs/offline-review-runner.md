# Offline review runner

Run the synthetic review suite with:

```bash
node --test tests/review-runner.test.mjs
node scripts/run-fixture-review.mjs fixtures/prompt-injection.json
```

The runner emits a `review-receipt.v1` document to stdout. It never contacts
GitHub, writes an artifact, repeats untrusted fixture content, or exposes
approval, merge, or thread-resolution authority.
