# Read-only collector

`node scripts/collect-review-packet.mjs` emits a `review-packet.v1` JSON
packet. It has two modes:

```bash
node scripts/collect-review-packet.mjs --fixture fixtures/prompt-injection.json
gh auth status
node scripts/collect-review-packet.mjs --repo OWNER/REPO --pr 123
```

The live mode performs one metadata-only `gh pr view` read and binds the output
to full base and head SHAs. It does not fetch arbitrary URLs, write files,
comment on GitHub, approve, merge, resolve threads, or authorize execution.

`--output` is deliberately unsupported in this first collector layer. The
artifact writer and receipt persistence path require their own review.
