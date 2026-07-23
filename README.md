# HUMMBL Independent Reviewer

Greenfield control plane for a bounded, evidence-first GitHub pull-request
reviewer. This repository is intentionally separate from the repositories it
reviews.

## Status

The repository is in bootstrap phase. Layers 1–5 have initial implementations.
No approval path, merge path, or deployment exists yet.

## Boundary

The reviewer may inspect explicitly allowlisted repository and pull-request
evidence and produce a structured review receipt. It must not approve its own
work, merge or push target changes, resolve review threads, authorize
deployment, or perform external reconnaissance.

The implementation/runtime repository is
[`hummbl-dev/hummbl-agent`](https://github.com/hummbl-dev/hummbl-agent). This
repository is the independent reviewer control plane and is not a replacement
for human or non-author review.

## Provenance

- Implementation issue: [`hummbl-dev/hummbl-agent#295`](https://github.com/hummbl-dev/hummbl-agent/issues/295)
- Evidence protocol work: [`hummbl-dev/hummbl-agent#291`](https://github.com/hummbl-dev/hummbl-agent/issues/291)

## Planned layers

1. Review packet schema and receipt contract.
2. Synthetic adversarial fixtures and threat model.
3. Read-only GitHub inspection and offline review runner.
4. Manual, allowlisted GitHub workflow with bounded rate and concurrency.
5. Dispatch protocol for cross-agent review requests.
6. Review request poller for automated fleet alerting.

Layers 3 and 4 are implemented by the review runner
(`scripts/review_runner.py`). See
[`docs/review-runner-setup.md`](docs/review-runner-setup.md).

All layers require branch-scoped changes, reviewable pull requests, and an
independent review gate before adoption.

## Dispatch protocol

Author agents request reviews via the [dispatch
protocol](docs/dispatch-protocol.md). Two channels are supported:

- **Coordination bus**: post a `PROPOSAL` message with a `REVIEW_REQUEST`
  marker and the canonical `review-request.v1` JSON payload.
- **GitHub issue**: open an issue using the `Review Request` template.

Requests conform to [`review-request.schema.json`](schemas/review-request.schema.json).
The reviewer validates, checks the repository allowlist, verifies
non-circumvention (no same-model self-review), collects evidence, and emits a
`review-receipt.v1` as a PR comment. See the full protocol at
[`docs/dispatch-protocol.md`](docs/dispatch-protocol.md).
