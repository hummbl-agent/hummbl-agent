# HUMMBL Independent Reviewer

Greenfield control plane for a bounded, evidence-first GitHub pull-request
reviewer. This repository is intentionally separate from the repositories it
reviews.

## Status

The repository is in bootstrap phase. No reviewer runtime, GitHub integration,
automatic comment publisher, approval path, merge path, or deployment exists
yet.

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

All layers require branch-scoped changes, reviewable pull requests, and an
independent review gate before adoption.

## Review request poller

A stdlib-only Python script (`scripts/review_request_poller.py`) scans the
coordination bus for pending `REVIEW_REQUEST` messages and alerts the fleet
on state transitions and bounded escalation intervals. Validated manually
but **not deployment-authorized** — no recurring scheduled task is active.
Defaults to dry-run (read-only). See [`docs/poller-setup.md`](docs/poller-setup.md)
for details.
