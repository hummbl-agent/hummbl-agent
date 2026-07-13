# Independent Reviewer Threat Model v0.1

## Protected assets

- Repository and pull-request integrity.
- Review authority boundaries and merge gates.
- Credentials and private review data.
- Evidence provenance, hashes, and receipts.
- Provider and GitHub rate-limit budgets.

## Trust boundaries

1. Target repository content is untrusted data.
2. GitHub API responses are evidence, not policy.
3. The reviewer runtime is separate from the repository under review.
4. A write-capable implementation collaborator is not automatically an
   independent reviewer or merge authority.
5. Human/operator approval remains outside the reviewer runtime.

## Threats and controls

| Threat | Required control |
|---|---|
| Prompt injection in README, issue, diff, or comment | Treat content as data; fixture-test refusal to follow it. |
| Reviewer self-approval | Compare reviewer and author identities; structurally deny approval. |
| Stale or mixed-base evidence | Bind every packet to exact base/head SHAs and block mismatch. |
| Secret leakage in review output | Redact before persistence/output; never commit credentials. |
| Duplicate evidence lineage | Record source identity and independence lineage. |
| Rate-limit exhaustion | Stagger requests, cap concurrency, and record retries. |
| Excess reviewer authority | Use read-only defaults and deny merge/push/thread-resolution operations. |
| Model overclaiming | Require evidence IDs, limitations, confidence, and contradiction states. |

## Non-goals

This threat model does not claim sandbox escape resistance, production security,
universal review correctness, or permission safety beyond the controls that are
implemented and tested.
