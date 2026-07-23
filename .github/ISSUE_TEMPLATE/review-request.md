---
name: Review Request
about: Request an independent review from the hummbl-agent reviewer
title: "Review request: <owner/repo>#<PR>"
labels: review-request
---

## Review Request

Fill in all required fields. See `schemas/review-request.schema.json` for the
canonical schema and `docs/dispatch-protocol.md` for the full protocol.

Paste a valid `review-request.v1` JSON object below. Both channels (bus and
issue) must carry the same canonical object.

### Required JSON

```json
{
  "schema_version": "review-request.v1",
  "request_id": "R-NNN",
  "target": {
    "repository": "owner/name",
    "pull_request": 0,
    "base_sha": "0000000000000000000000000000000000000000",
    "head_sha": "0000000000000000000000000000000000000000",
    "url": "https://github.com/owner/name/pull/0"
  },
  "requested_mode": "read_only",
  "author_identity": "github-login-of-pr-author",
  "requester_model": "model-name-of-authoring-agent",
  "author_provenance": {
    "bus_receipt_id": "NNNNNNNNNNNNNNNN-XXXXXXXXXXXXXXXX",
    "bus_receipt_sender": "devin",
    "bus_receipt_timestamp": "2026-01-01T00:00:00Z"
  }
}
```

### Field reference

| Field | Required | Description |
|---|---|---|
| `schema_version` | yes | Must be `review-request.v1` |
| `request_id` | yes | Unique ID: `R-NNN` |
| `target.repository` | yes | `owner/name` |
| `target.pull_request` | yes | PR number (integer) |
| `target.base_sha` | yes | 40-char hex SHA |
| `target.head_sha` | yes | 40-char hex SHA |
| `target.url` | no | Full PR URL |
| `requested_mode` | yes | `fixture`, `read_only`, or `write_review` |
| `author_identity` | yes | GitHub login of the PR author |
| `requester_model` | yes | Model name of the agent that authored the PR |
| `author_provenance.bus_receipt_id` | yes | Bus receipt ID proving author model provenance |
| `author_provenance.bus_receipt_sender` | yes | Bus sender identity for the provenance receipt |
| `author_provenance.bus_receipt_timestamp` | yes | Timestamp of the provenance receipt |
| `review_focus` | no | Array of areas to review |
| `operator_waiver` | no | `{ "granted": true, "reason": "..." }` — only for low-risk same-model review |
| `requested_at` | no | ISO 8601 timestamp |

### Notes

<!-- any context the reviewer should know (but NOT secrets or credentials) -->
