---
name: Review Request
about: Request an independent review from the hummbl-agent reviewer
title: "Review request: <owner/repo>#<PR>"
labels: review-request
---

## Review Request

Fill in all required fields. See `schemas/review-request.schema.json` for the
canonical schema and `docs/dispatch-protocol.md` for the full protocol.

### Required

- **request_id**: R-NNN
- **repository**: owner/name
- **pull_request**: NN
- **base_sha**: 40-char hex
- **head_sha**: 40-char hex
- **requested_mode**: fixture | read_only | write_review
- **author_identity**: GitHub login of the PR author
- **requester_model**: model name of the agent that authored the PR

### Optional

- **review_focus**: areas to review (e.g. security, schema, performance)
- **operator_waiver**: { granted: true/false, reason: "..." } — only for
  low-risk same-model review; not permitted for security, governance, schema,
  integration, or authority changes
- **requested_at**: ISO 8601 timestamp

### PR URL

<!-- paste the full PR URL here -->

### Notes

<!-- any context the reviewer should know (but NOT secrets or credentials) -->
