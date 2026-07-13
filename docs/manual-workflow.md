# Manual read-only GitHub workflow

The `Manual read-only review` workflow is intentionally `workflow_dispatch`
only. It accepts one repository and pull-request number, checks the explicit
allowlist in `config/review-policy.json`, waits for the configured stagger
interval, and uploads a SHA-bound review packet as an artifact.

It does not run automatically on pull requests and does not post comments,
approve, merge, resolve threads, deploy, or authorize external effects. The
workflow token has read-only repository and pull-request permissions. A private
target that is not visible to the workflow token must fail closed.
