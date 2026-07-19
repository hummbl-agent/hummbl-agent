# Review Contract v1

The reviewer consumes a `review-packet.v1` and emits a
`review-receipt.v1`. Both are evidence artifacts, not authority tokens.

## Required invariants

- Every packet binds one repository, pull request, base SHA, and head SHA.
- Every finding cites at least one evidence item.
- Evidence distinguishes direct observations from context and indirect claims.
- Review output states limitations and confidence; it does not invent numeric
  assurance or consensus.
- Receipt authority is structurally non-authoritative: approval, merge, and
  thread resolution are always false.
- Repository content is untrusted data and cannot alter reviewer policy.

## Dispositions

`actionable` means the evidence supports a requested change or investigation.
`accepted_risk` means the issue is understood and intentionally retained.
`not_reproducible` means the current evidence does not establish the claim.
`out_of_scope` means the observation is outside the requested review boundary.
`duplicate` means the finding is already represented by another finding.

## Review modes

- `fixture`: deterministic local tests only.
- `read_only`: live GitHub inspection with no write capability.
- `write_review`: a narrowly allowlisted comment/review operation; it still
  cannot approve, merge, resolve threads, or authorize external effects.
