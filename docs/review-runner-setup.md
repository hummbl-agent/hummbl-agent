# Review Runner Setup

The review runner (`scripts/review_runner.py`) is the actor that performs
independent reviews. It consumes a review-request, collects evidence from
GitHub, validates against fixtures and secret-scan rules, assembles a
`review-packet.v1`, emits a `review-receipt.v1`, and optionally posts the
receipt as a PR comment and a `REVIEW_COMPLETE` bus message.

This is planned layer 3 (read-only GitHub inspection and offline review
runner) and layer 4 (manual, allowlisted GitHub workflow with bounded rate
and concurrency) from the README.

## Deployment status

| Status | Description |
|---|---|
| **Validated manually** | Script compiles, runs in fixture mode, runs in read_only dry-run mode against a live PR. Produces schema-conformant packets and receipts. Non-circumvention check verified (blocks same-model, blocks same-identity). |
| **NOT deployment-authorized** | No automated invocation is authorized. The runner must be invoked manually by an independent agent or the operator until recurring deployment is approved. |

## Modes

| Mode | GitHub API | Auth switch | PR comment | Bus post | Use case |
|---|---|---|---|---|---|
| `fixture` | No | No | No | No | Deterministic local tests against synthetic fixtures |
| `read_only` | Yes (read) | Yes | No | No | Live evidence collection, receipt to stdout/file |
| `write_review` | Yes (read) | Yes | Yes | Yes | Full review: evidence + receipt posted as PR comment + bus |

All modes produce a `review-packet.v1` and `review-receipt.v1`. The
receipt always has `can_approve=false`, `can_merge=false`,
`can_resolve_threads=false` — these are structural constants.

## Non-circumvention

The runner checks two conditions before proceeding:

1. **Same GitHub identity**: If `author_identity == reviewer_identity`,
   the review is blocked.
2. **Same model**: If `requester_model == reviewer_model`, the review is
   blocked unless an `operator_waiver` with `granted: true` is present in
   the request.

A blocked review still produces a receipt with `verdict: blocked` — the
block is recorded as evidence, not silently dropped.

## Evidence collection (read_only and write_review)

The runner collects:

| Evidence | Source | Kind |
|---|---|---|
| PR metadata (state, draft, author, base/head SHA) | `gh api repos/{repo}/pulls/{pr}` | `review` |
| Full diff | `gh api repos/{repo}/pulls/{pr}` (Accept: diff) | `diff` |
| Changed files list | `gh api repos/{repo}/pulls/{pr}/files` | `file` |
| CI check runs | `gh api repos/{repo}/commits/{head_sha}/check-runs` | `check` |

All evidence is treated as untrusted data per `AGENTS.md`. The runner
verifies that the PR's actual base/head SHA matches the request's
base/head SHA. Mismatches produce a `blocked` verdict.

## Checks

The runner performs these checks on every review:

| Check | What it detects |
|---|---|
| Secret scan | GitHub tokens, AWS keys, OpenAI keys, PEM private keys, `*_TOKEN=`/`*_KEY=`/`*_SECRET=` patterns in diff and evidence |
| Prompt injection | "ignore instructions", "approve this PR", "reveal hidden", "you are now", "disregard policy" patterns in evidence text |
| Fixture validation | Runs all synthetic adversarial fixtures (prompt injection, stale base, secret-like text, conflicting evidence, self-review) |
| SHA verification | Base/head SHA in request must match PR's actual SHAs |

Secret matches are redacted in findings — the full secret value is never
reproduced in the packet or receipt.

## Usage

### Fixture mode (no GitHub, no auth, no writes)

```bash
python scripts/review_runner.py \
    --mode fixture \
    --repo fixture/example --pr 1 \
    --base-sha 1111111111111111111111111111111111111111 \
    --head-sha 2222222222222222222222222222222222222222 \
    --reviewer-identity hummbl-agent \
    --reviewer-model GPT-5-Codex \
    --author-identity hummbl-dev \
    --requester-model GLM-5.2-Devin \
    --dry-run
```

### Read-only mode (live evidence, no writes)

```bash
python scripts/review_runner.py \
    --mode read_only \
    --repo hummbl-agent/hummbl-agent --pr 7 \
    --base-sha af26f05e21b46cc397d3e637287006557a3b9622 \
    --head-sha e8f809495d412fc7c59260e786a7914b98ae59a8 \
    --reviewer-identity hummbl-agent \
    --reviewer-model GPT-5-Codex \
    --author-identity hummbl-dev \
    --requester-model GLM-5.2-Devin \
    --dry-run \
    --output receipts/pr7
```

### Write-review mode (post receipt as PR comment + bus)

```bash
python scripts/review_runner.py \
    --mode write_review \
    --repo hummbl-agent/hummbl-agent --pr 7 \
    --base-sha af26f05e21b46cc397d3e637287006557a3b9622 \
    --head-sha e8f809495d412fc7c59260e786a7914b98ae59a8 \
    --reviewer-identity hummbl-agent \
    --reviewer-model GPT-5-Codex \
    --author-identity hummbl-dev \
    --requester-model GLM-5.2-Devin \
    --request-id R-001
```

### From a request file

```bash
python scripts/review_runner.py \
    --mode read_only \
    --request-file request.json \
    --reviewer-model GPT-5-Codex \
    --dry-run
```

## Auth handling

In `read_only` and `write_review` modes (without `--dry-run`), the runner:

1. Records the current `gh auth` identity
2. Switches to `hummbl-agent` via `gh auth switch -u hummbl-agent`
3. Performs all GitHub API calls under that identity
4. Restores the original identity in a `finally` block

With `--dry-run`, no auth switch occurs. The runner uses whatever token
is currently active for read-only API calls but does not post anything.

## Output

By default, the packet and receipt are printed to stdout as JSON. With
`--output <dir>`, they are written to `<dir>/review-packet.json` and
`<dir>/review-receipt.json`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Review completed (any verdict: pass, findings, blocked, inconclusive) |
| 2 | Operational failure (auth error, API error, invalid request) |

## Limitations

- The runner does not perform semantic code analysis. It collects
  evidence, runs deterministic checks (secret scan, prompt injection
  detection, fixture validation, SHA verification), and produces a
  structured receipt. Interpretation of findings is left to the
  reviewing agent or operator.
- Fixture findings appear in every review because `run_fixture_checks()`
  always runs. These are synthetic adversarial test results, not findings
  about the PR under review. A future version should separate fixture
  validation (pass/fail) from PR findings.
- The runner uses `gh api` subprocess calls, not a GitHub SDK. This keeps
  it stdlib-only but limits concurrency and error granularity.
- The `review-poller` bus identity is not registered. `REVIEW_COMPLETE`
  bus posts in `write_review` mode will fail until the identity is
  approved or the runner uses an authorized identity.
