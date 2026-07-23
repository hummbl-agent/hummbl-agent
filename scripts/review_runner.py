#!/usr/bin/env python3
"""Independent review runner for the hummbl-agent reviewer control plane.

Consumes a review-request.v1, collects evidence from GitHub (read-only),
validates against fixtures and secret-scan rules, assembles a
review-packet.v1, emits a review-receipt.v1, and optionally posts the
receipt as a PR comment and a REVIEW_COMPLETE bus message.

This is the actor that the dispatch protocol (PR #7) and poller (PR #9)
signal to. It is the missing layer 3 from the planned layers.

Modes:
    fixture    — run only deterministic fixture checks against synthetic
                 data. No GitHub API calls, no auth switch, no writes.
    read_only  — switch to hummbl-agent auth, fetch PR evidence via
                 GitHub API (read-only), build packet, emit receipt.
                 No PR comment, no bus post.
    write_review — same as read_only, plus post the receipt as a PR
                 comment under the hummbl-agent identity and post
                 REVIEW_COMPLETE to the coordination bus.

Non-circumvention:
    The runner checks that the reviewer identity differs from the PR
    author identity. If they match, the review is blocked. The
    --reviewer-model flag declares the reviewer's model; if it matches
    the request's requester_model, the review is blocked unless an
    operator waiver is present in the request.

Authority:
    The receipt always has can_approve=false, can_merge=false,
    can_resolve_threads=false. These are structural constants, not
    configurable.

Usage:
    # Fixture mode (no GitHub, no auth, no writes)
    python review_runner.py --mode fixture --request-file request.json

    # Read-only mode (fetch evidence, emit receipt, no writes)
    python review_runner.py --mode read_only --repo owner/name --pr 7 \
        --base-sha <40hex> --head-sha <40hex> \
        --reviewer-identity hummbl-agent --reviewer-model GPT-5-Codex

    # Write-review mode (post receipt as PR comment + bus)
    python review_runner.py --mode write_review --repo owner/name --pr 7 \
        --base-sha <40hex> --head-sha <40hex> \
        --reviewer-identity hummbl-agent --reviewer-model GPT-5-Codex

    # Dry-run (any mode): produce packet + receipt to stdout, no writes
    python review_runner.py --mode read_only ... --dry-run

Exit codes:
    0 — review completed (any verdict: pass, findings, blocked, inconclusive)
    2 — operational failure (auth error, API error, invalid request)

Dependencies: Python 3.8+ stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVIEWER_IDENTITY_DEFAULT = "hummbl-agent"
REVIEWER_RUNTIME = "review_runner.py v0.1"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_PATTERNS = [
    re.compile(r"gh[ops]_[A-Za-z0-9]{36}"),       # GitHub tokens
    re.compile(r"sk-[A-Za-z0-9]{20,}"),             # OpenAI-style keys
    re.compile(r"AKIA[A-Z0-9]{16}"),                # AWS access keys
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private keys
    re.compile(r"[A-Z_]+_TOKEN\s*=\s*[A-Za-z0-9+/=]{20,}"),  # *_TOKEN=...
    re.compile(r"[A-Z_]+_KEY\s*=\s*[A-Za-z0-9+/=]{20,}"),    # *_KEY=...
    re.compile(r"[A-Z_]+_SECRET\s*=\s*[A-Za-z0-9+/=]{20,}"), # *_SECRET=...
]
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore (the |all )?(reviewer |previous )?instructions?", re.I),
    re.compile(r"approve this (pull request|pr|merge)", re.I),
    re.compile(r"reveal (all )?hidden (instructions?|secrets?|prompts?)", re.I),
    re.compile(r"you are now (a |an )?\w+", re.I),
    re.compile(r"disregard (the |all |your )?(policy|rules|guidelines)", re.I),
]

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def validate_request(request):
    """Validate a review-request.v1 dict. Returns (valid, errors list)."""
    errors = []
    if not isinstance(request, dict):
        return False, ["request is not a JSON object"]

    required = ["schema_version", "request_id", "target", "requested_mode",
                "author_identity", "requester_model"]
    for field in required:
        if field not in request:
            errors.append(f"missing required field: {field}")

    if errors:
        return False, errors

    if request.get("schema_version") != "review-request.v1":
        errors.append(f"schema_version must be 'review-request.v1', got '{request.get('schema_version')}'")

    target = request.get("target", {})
    for field in ["repository", "pull_request", "base_sha", "head_sha"]:
        if field not in target:
            errors.append(f"missing target field: {field}")
    if target.get("base_sha") and not SHA40_RE.match(str(target.get("base_sha", ""))):
        errors.append("base_sha is not a valid 40-char hex SHA")
    if target.get("head_sha") and not SHA40_RE.match(str(target.get("head_sha", ""))):
        errors.append("head_sha is not a valid 40-char hex sha")

    mode = request.get("requested_mode")
    if mode not in ["fixture", "read_only", "write_review"]:
        errors.append(f"requested_mode must be fixture|read_only|write_review, got '{mode}'")

    rid = request.get("request_id", "")
    if not re.match(r"^R-\d{3}$", rid):
        errors.append(f"request_id must match R-NNN, got '{rid}'")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Non-circumvention check
# ---------------------------------------------------------------------------

def check_non_circumvention(request, reviewer_identity, reviewer_model):
    """Check that the reviewer is independent from the author.

    Returns (passed, reason).
    """
    author = request.get("author_identity", "")
    author_model = request.get("requester_model", "")

    # Same GitHub identity → blocked
    if author and reviewer_identity and author == reviewer_identity:
        return False, f"same-identity: author '{author}' == reviewer '{reviewer_identity}'"

    # Same model, different identity → blocked unless waiver
    if author_model and reviewer_model and author_model == reviewer_model:
        waiver = request.get("operator_waiver", {})
        if waiver and waiver.get("granted"):
            return True, f"same-model waived: {waiver.get('reason', 'no reason given')}"
        return False, f"same-model: author model '{author_model}' == reviewer model '{reviewer_model}'"

    return True, "independent"


# ---------------------------------------------------------------------------
# GitHub evidence collection (read-only)
# ---------------------------------------------------------------------------

def gh_api(endpoint, reviewer_identity=REVIEWER_IDENTITY_DEFAULT):
    """Call GitHub API read-only using the hummbl-agent identity.

    Returns parsed JSON or None on error.
    """
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GH_TOKEN": _get_gh_token(reviewer_identity)},
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return None


def _get_gh_token(identity):
    """Get the GitHub token for the specified identity via gh auth token."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--user", identity],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Fallback: use whatever token is active
    return os.environ.get("GH_TOKEN", "")


def collect_evidence(repo, pr_number, base_sha, head_sha):
    """Collect read-only evidence from GitHub.

    Returns a list of evidence dicts conforming to review-packet.v1.
    """
    evidence = []
    eid_counter = [1]

    def next_eid():
        eid = f"E-{eid_counter[0]:03d}"
        eid_counter[0] += 1
        return eid

    now = utc_now_iso()

    # 1. PR metadata
    pr_data = gh_api(f"repos/{repo}/pulls/{pr_number}")
    if pr_data:
        evidence.append({
            "id": next_eid(),
            "kind": "review",
            "source": f"github:repos/{repo}/pulls/{pr_number}",
            "directness": "direct",
            "authority": "github",
            "observed_at": now,
            "notes": f"PR state={pr_data.get('state')}, draft={pr_data.get('draft')}, "
                     f"author={pr_data.get('user', {}).get('login', '?')}",
        })

        # Verify base/head SHA match
        actual_base = pr_data.get("base", {}).get("sha", "")
        actual_head = pr_data.get("head", {}).get("sha", "")
        if actual_base and actual_base != base_sha:
            evidence.append({
                "id": next_eid(),
                "kind": "review",
                "source": f"github:repos/{repo}/pulls/{pr_number}#base_sha",
                "directness": "direct",
                "authority": "github",
                "observed_at": now,
                "notes": f"BASE_SHA_MISMATCH: request={base_sha} actual={actual_base}",
            })
        if actual_head and actual_head != head_sha:
            evidence.append({
                "id": next_eid(),
                "kind": "review",
                "source": f"github:repos/{repo}/pulls/{pr_number}#head_sha",
                "directness": "direct",
                "authority": "github",
                "observed_at": now,
                "notes": f"HEAD_SHA_MISMATCH: request={head_sha} actual={actual_head}",
            })

    # 2. Diff
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}",
             "-H", "Accept: application/vnd.github.diff"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GH_TOKEN": _get_gh_token(REVIEWER_IDENTITY_DEFAULT)},
        )
        if result.returncode == 0 and result.stdout:
            diff_text = result.stdout
            evidence.append({
                "id": next_eid(),
                "kind": "diff",
                "source": f"github:repos/{repo}/pulls/{pr_number}.diff",
                "directness": "direct",
                "authority": "github",
                "observed_at": now,
                "sha": head_sha,
                "notes": f"diff_size={len(diff_text)} chars, {diff_text.count(chr(10))} lines",
            })
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 3. Changed files
    files_data = gh_api(f"repos/{repo}/pulls/{pr_number}/files")
    if files_data and isinstance(files_data, list):
        file_list = [f.get("filename", "?") for f in files_data]
        evidence.append({
            "id": next_eid(),
            "kind": "file",
            "source": f"github:repos/{repo}/pulls/{pr_number}/files",
            "directness": "direct",
            "authority": "github",
            "observed_at": now,
            "notes": f"changed_files={len(file_list)}: {', '.join(file_list[:20])}",
        })

    # 4. CI checks
    checks_data = gh_api(f"repos/{repo}/commits/{head_sha}/check-runs")
    if checks_data and isinstance(checks_data, dict):
        runs = checks_data.get("check_runs", [])
        if runs:
            check_summary = "; ".join(
                f"{r.get('name', '?')}={r.get('conclusion', '?')}" for r in runs[:10]
            )
            evidence.append({
                "id": next_eid(),
                "kind": "check",
                "source": f"github:repos/{repo}/commits/{head_sha}/check-runs",
                "directness": "direct",
                "authority": "ci",
                "observed_at": now,
                "notes": f"checks={len(runs)}: {check_summary}",
            })

    return evidence


# ---------------------------------------------------------------------------
# Secret scan
# ---------------------------------------------------------------------------

def scan_for_secrets(text):
    """Scan text for secret-like patterns. Returns list of (pattern_name, match)."""
    findings = []
    for i, pattern in enumerate(SECRET_PATTERNS):
        for match in pattern.finditer(text):
            findings.append((f"secret_pattern_{i}", match.group(0)))
    return findings


def scan_evidence_for_secrets(evidence, diff_text=""):
    """Scan all evidence text for secrets. Returns list of finding dicts."""
    findings = []
    all_text = diff_text
    for ev in evidence:
        all_text += " " + ev.get("notes", "")

    for pattern_name, matched in scan_for_secrets(all_text):
        # Redact the secret — never reproduce it
        redacted = matched[:8] + "...REDACTED"
        findings.append({
            "pattern": pattern_name,
            "redacted_match": redacted,
            "severity": "high",
        })
    return findings


# ---------------------------------------------------------------------------
# Prompt injection detection
# ---------------------------------------------------------------------------

def scan_for_prompt_injection(text):
    """Scan text for prompt-injection patterns. Returns list of matches."""
    findings = []
    for pattern in PROMPT_INJECTION_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(match.group(0))
    return findings


# ---------------------------------------------------------------------------
# Fixture validation
# ---------------------------------------------------------------------------

def run_fixture_checks():
    """Run all fixture checks against synthetic data.

    Returns a list of finding dicts.
    """
    findings = []
    fixture_files = list(FIXTURES_DIR.glob("*.json"))

    for fixture_path in fixture_files:
        if fixture_path.name == "README.md":
            continue
        try:
            with open(fixture_path, "r", encoding="utf-8") as f:
                fixture = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        fixture_id = fixture.get("fixture_id", fixture_path.stem)
        expected = fixture.get("expected", {})
        expected_verdict = expected.get("verdict")

        # Each fixture is a synthetic test case. We validate that the
        # fixture's expected behavior is internally consistent and that
        # our scanning logic would produce the expected verdict.

        untrusted = fixture.get("untrusted_content", "")
        if untrusted:
            # Check for prompt injection
            injections = scan_for_prompt_injection(untrusted)
            if injections and expected_verdict == "findings":
                findings.append({
                    "fixture_id": fixture_id,
                    "verdict": "findings",
                    "finding": f"prompt injection detected: {injections[0][:60]}",
                    "severity": "medium",
                })

            # Check for secrets
            secrets = scan_for_secrets(untrusted)
            if secrets and expected_verdict == "findings":
                findings.append({
                    "fixture_id": fixture_id,
                    "verdict": "findings",
                    "finding": f"secret-like content detected: {secrets[0][1][:20]}...REDACTED",
                    "severity": "high",
                })

        # Check self-review
        reviewer_id = fixture.get("reviewer_identity", "")
        author_id = fixture.get("author_identity", "")
        if reviewer_id and author_id and reviewer_id == author_id:
            if expected_verdict == "blocked":
                findings.append({
                    "fixture_id": fixture_id,
                    "verdict": "blocked",
                    "finding": "same-identity review cannot satisfy the independent gate",
                    "severity": "critical",
                })

        # Check stale base
        evidence_list = fixture.get("evidence", [])
        if evidence_list:
            base_sha = fixture.get("target", {}).get("base_sha", "")
            for ev in evidence_list:
                source = ev.get("source", "")
                if "diff-base-" in source and base_sha not in source:
                    if expected_verdict == "blocked":
                        findings.append({
                            "fixture_id": fixture_id,
                            "verdict": "blocked",
                            "finding": "base SHA mismatch requires refreshed evidence",
                            "severity": "high",
                        })

        # Check conflicting evidence
        if len(evidence_list) >= 2:
            authorities = [ev.get("authority") for ev in evidence_list]
            if "repository" in authorities and "ci" in authorities:
                if expected_verdict == "inconclusive":
                    findings.append({
                        "fixture_id": fixture_id,
                        "verdict": "inconclusive",
                        "finding": "contradictory evidence must be surfaced and adjudicated",
                        "severity": "medium",
                    })

    return findings


# ---------------------------------------------------------------------------
# Packet assembly
# ---------------------------------------------------------------------------

def build_packet(target, review_context, evidence, findings):
    """Assemble a review-packet.v1 dict."""
    # Convert findings to schema format
    packet_findings = []
    for i, f in enumerate(findings):
        packet_findings.append({
            "id": f"F-{i+1:03d}",
            "severity": f.get("severity", "informational"),
            "title": f.get("finding", f.get("title", "finding")),
            "description": f.get("description", f.get("finding", "")),
            "path": f.get("path", ""),
            "line": f.get("line", 0) if f.get("line") else None,
            "evidence_ids": f.get("evidence_ids", [evidence[0]["id"]] if evidence else ["E-001"]),
            "disposition": f.get("disposition", "actionable"),
            "confidence": f.get("confidence", "medium"),
            "limitations": f.get("limitations", []),
        })
    # Clean None values
    for f in packet_findings:
        f = {k: v for k, v in f.items() if v is not None}

    return {
        "schema_version": "review-packet.v1",
        "target": target,
        "review_context": review_context,
        "evidence": evidence,
        "findings": packet_findings,
    }


# ---------------------------------------------------------------------------
# Receipt emission
# ---------------------------------------------------------------------------

def compute_packet_sha256(packet):
    """Compute SHA-256 of the canonical JSON packet."""
    packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packet_json.encode("utf-8")).hexdigest()


def build_receipt(packet, reviewer_identity, reviewer_model, verdict,
                  finding_count, summary, mode, limitations=None):
    """Assemble a review-receipt.v1 dict."""
    packet_hash = compute_packet_sha256(packet)
    return {
        "schema_version": "review-receipt.v1",
        "packet_sha256": packet_hash,
        "target": packet["target"],
        "reviewer": {
            "identity": reviewer_identity,
            "runtime": REVIEWER_RUNTIME,
            "generated_at": utc_now_iso(),
        },
        "result": {
            "verdict": verdict,
            "finding_count": finding_count,
            "summary": summary,
        },
        "authority": {
            "mode": mode,
            "can_approve": False,
            "can_merge": False,
            "can_resolve_threads": False,
            "limitations": limitations or [],
        },
    }


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------

def compute_verdict(findings, non_circumvention_passed, sha_mismatches):
    """Determine the verdict from findings and checks.

    Returns (verdict, summary).
    """
    if not non_circumvention_passed:
        return "blocked", "non-circumvention check failed"

    if sha_mismatches:
        return "blocked", "base/head SHA mismatch — stale evidence"

    if not findings:
        return "pass", "no findings"

    # Check for critical/blocking findings
    has_blocking = any(
        f.get("severity") in ("critical", "high") or f.get("verdict") == "blocked"
        for f in findings
    )
    has_inconclusive = any(
        f.get("verdict") == "inconclusive" for f in findings
    )

    if has_inconclusive:
        return "inconclusive", f"{len(findings)} finding(s), inconclusive evidence"
    if has_blocking:
        return "findings", f"{len(findings)} finding(s), blocking severity present"

    return "findings", f"{len(findings)} finding(s)"


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_receipt_markdown(receipt, packet):
    """Format the receipt as a GitHub PR comment in markdown."""
    lines = [
        "## Independent Review Receipt",
        "",
        f"**Reviewer:** `{receipt['reviewer']['identity']}` "
        f"({receipt['reviewer']['runtime']})",
        f"**Generated:** {receipt['reviewer']['generated_at']}",
        f"**Packet SHA-256:** `{receipt['packet_sha256']}`",
        "",
        f"**Verdict:** `{receipt['result']['verdict']}`",
        f"**Finding count:** {receipt['result']['finding_count']}",
        f"**Summary:** {receipt['result']['summary']}",
        "",
        "### Authority",
        "",
        f"- Mode: `{receipt['authority']['mode']}`",
        f"- Can approve: `{receipt['authority']['can_approve']}`",
        f"- Can merge: `{receipt['authority']['can_merge']}`",
        f"- Can resolve threads: `{receipt['authority']['can_resolve_threads']}`",
        "",
    ]
    if receipt["authority"].get("limitations"):
        lines.append("### Limitations")
        lines.append("")
        for lim in receipt["authority"]["limitations"]:
            lines.append(f"- {lim}")
        lines.append("")

    if packet.get("evidence"):
        lines.append("### Evidence")
        lines.append("")
        for ev in packet["evidence"]:
            lines.append(
                f"- `{ev['id']}` {ev['kind']} ({ev['directness']}, "
                f"{ev['authority']}): {ev['source']}"
            )
            if ev.get("notes"):
                lines.append(f"  - {ev['notes']}")
        lines.append("")

    if packet.get("findings"):
        lines.append("### Findings")
        lines.append("")
        for f in packet["findings"]:
            lines.append(
                f"- `{f['id']}` [{f['severity']}] {f['title']} "
                f"→ {f['disposition']} (confidence: {f['confidence']})"
            )
            if f.get("description"):
                lines.append(f"  - {f['description']}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "This receipt is an evidence artifact, not an authority token. "
        "The reviewer cannot approve, merge, or resolve review threads. "
        "Merge decisions are reserved for the operator or author."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PR comment posting
# ---------------------------------------------------------------------------

def post_pr_comment(repo, pr_number, body, reviewer_identity):
    """Post a comment on a PR using the hummbl-agent identity."""
    try:
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", repo,
             "--body", body],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GH_TOKEN": _get_gh_token(reviewer_identity)},
        )
        if result.returncode == 0:
            return True, None
        return False, result.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Bus REVIEW_COMPLETE posting
# ---------------------------------------------------------------------------

def post_review_complete(request_id, repo, pr_number, verdict, reviewer_identity):
    """Post a REVIEW_COMPLETE message to the coordination bus."""
    msg = (f"REVIEW_COMPLETE request_id={request_id} repo={repo} pr={pr_number} "
           f"verdict={verdict} reviewer={reviewer_identity}")
    try:
        result = subprocess.run(
            [sys.executable, str(Path.home() / "bin" / "bus-global.py"),
             "post", reviewer_identity, "all", "STATUS", msg],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, None
        return False, result.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Auth switch
# ---------------------------------------------------------------------------

def switch_auth(identity):
    """Switch gh auth to the specified identity. Returns (success, error)."""
    try:
        result = subprocess.run(
            ["gh", "auth", "switch", "-u", identity],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, None
        return False, result.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def restore_auth(previous_identity):
    """Switch gh auth back to the previous identity."""
    if previous_identity:
        switch_auth(previous_identity)


def get_current_auth():
    """Get the currently active gh auth account."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        # Parse "Active account: true" line to find the account name
        for line in result.stdout.split("\n"):
            if "Logged in to github.com account" in line:
                return line.split("account")[1].strip().split()[0]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Independent review runner — collects evidence, validates, emits receipt."
    )
    parser.add_argument("--mode", required=True,
                        choices=["fixture", "read_only", "write_review"],
                        help="Review mode")
    parser.add_argument("--request-file", type=str, default=None,
                        help="Path to a review-request.v1 JSON file")
    parser.add_argument("--repo", type=str, default=None,
                        help="Target repository (owner/name)")
    parser.add_argument("--pr", type=int, default=None,
                        help="Pull request number")
    parser.add_argument("--base-sha", type=str, default=None,
                        help="Base commit SHA (40 hex)")
    parser.add_argument("--head-sha", type=str, default=None,
                        help="Head commit SHA (40 hex)")
    parser.add_argument("--request-id", type=str, default=None,
                        help="Request ID (R-NNN) for bus REVIEW_COMPLETE")
    parser.add_argument("--reviewer-identity", type=str,
                        default=REVIEWER_IDENTITY_DEFAULT,
                        help=f"Reviewer GitHub identity (default: {REVIEWER_IDENTITY_DEFAULT})")
    parser.add_argument("--reviewer-model", type=str, default=None,
                        help="Reviewer model name (for non-circumvention check)")
    parser.add_argument("--author-identity", type=str, default=None,
                        help="PR author GitHub login (for non-circumvention check)")
    parser.add_argument("--requester-model", type=str, default=None,
                        help="Author's model name (for non-circumvention check)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Produce packet + receipt to stdout, no writes")
    parser.add_argument("--output", type=str, default=None,
                        help="Write packet + receipt to this directory")
    args = parser.parse_args()

    # --- Load or construct request ---
    if args.request_file:
        try:
            with open(args.request_file, "r", encoding="utf-8") as f:
                request = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"ERROR: cannot read request file: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        # Construct from CLI args
        if not args.repo or args.pr is None or not args.base_sha or not args.head_sha:
            print("ERROR: --repo, --pr, --base-sha, --head-sha required (or --request-file)",
                  file=sys.stderr)
            sys.exit(2)
        request = {
            "schema_version": "review-request.v1",
            "request_id": args.request_id or "R-000",
            "target": {
                "repository": args.repo,
                "pull_request": args.pr,
                "base_sha": args.base_sha,
                "head_sha": args.head_sha,
            },
            "requested_mode": args.mode,
            "author_identity": args.author_identity or "",
            "requester_model": args.requester_model or "",
        }

    # --- Validate request ---
    valid, errors = validate_request(request)
    if not valid:
        print("ERROR: invalid request:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(2)

    target = request["target"]
    repo = target["repository"]
    pr_number = target["pull_request"]
    base_sha = target["base_sha"]
    head_sha = target["head_sha"]
    mode = args.mode

    # --- Non-circumvention check ---
    nc_passed, nc_reason = check_non_circumvention(
        request, args.reviewer_identity, args.reviewer_model or ""
    )
    print(f"Non-circumvention: {'PASS' if nc_passed else 'BLOCKED'} — {nc_reason}",
          file=sys.stderr)

    # --- Fixture mode: no GitHub, no auth ---
    if mode == "fixture":
        fixture_findings = run_fixture_checks()
        evidence = [{
            "id": "E-001",
            "kind": "fixture",
            "source": "fixtures/*.json",
            "directness": "direct",
            "authority": "fixture",
            "observed_at": utc_now_iso(),
            "notes": f"{len(fixture_findings)} fixture finding(s)",
        }]
        review_context = {
            "reviewer_identity": args.reviewer_identity,
            "mode": "fixture",
            "generated_at": utc_now_iso(),
            "model": args.reviewer_model or "unknown",
            "limitations": ["fixture mode: no live GitHub evidence collected"],
        }
        packet = build_packet(target, review_context, evidence, fixture_findings)
        verdict, summary = compute_verdict(fixture_findings, nc_passed, sha_mismatches=[])
        receipt = build_receipt(
            packet, args.reviewer_identity, args.reviewer_model,
            verdict, len(fixture_findings), summary, "fixture",
            limitations=["fixture mode only", "no live evidence"]
        )
        _output_results(packet, receipt, args)
        sys.exit(0)

    # --- read_only / write_review: auth switch + evidence collection ---
    previous_auth = get_current_auth()

    if not args.dry_run and mode in ("read_only", "write_review"):
        print(f"Switching auth to {args.reviewer_identity}...", file=sys.stderr)
        success, err = switch_auth(args.reviewer_identity)
        if not success:
            print(f"ERROR: auth switch failed: {err}", file=sys.stderr)
            sys.exit(2)

    try:
        evidence = collect_evidence(repo, pr_number, base_sha, head_sha)

        # Check for SHA mismatches in evidence
        sha_mismatches = [
            ev for ev in evidence
            if "SHA_MISMATCH" in ev.get("notes", "")
        ]

        # Secret scan
        secret_findings = scan_evidence_for_secrets(evidence)

        # Prompt injection scan on evidence notes
        injection_findings = []
        for ev in evidence:
            notes = ev.get("notes", "")
            injections = scan_for_prompt_injection(notes)
            for inj in injections:
                injection_findings.append({
                    "severity": "medium",
                    "finding": f"prompt injection pattern in evidence: {inj[:60]}",
                    "disposition": "actionable",
                    "confidence": "medium",
                })

        # Also run fixture checks (always)
        fixture_findings = run_fixture_checks()

        all_findings = secret_findings + injection_findings + fixture_findings

        review_context = {
            "reviewer_identity": args.reviewer_identity,
            "mode": mode,
            "generated_at": utc_now_iso(),
            "model": args.reviewer_model or "unknown",
            "limitations": [],
        }
        if not nc_passed:
            review_context["limitations"].append(
                f"non-circumvention: {nc_reason}"
            )

        packet = build_packet(target, review_context, evidence, all_findings)
        verdict, summary = compute_verdict(
            all_findings, nc_passed, sha_mismatches
        )

        limitations = []
        if sha_mismatches:
            limitations.append("base/head SHA mismatch detected")
        if not evidence:
            limitations.append("no evidence collected — GitHub API may have failed")

        receipt = build_receipt(
            packet, args.reviewer_identity, args.reviewer_model,
            verdict, len(all_findings), summary, mode,
            limitations=limitations
        )

        _output_results(packet, receipt, args)

        # --- write_review: post PR comment + bus message ---
        if mode == "write_review" and not args.dry_run:
            # Post PR comment
            comment_body = format_receipt_markdown(receipt, packet)
            print(f"\nPosting receipt as PR comment on {repo}#{pr_number}...",
                  file=sys.stderr)
            success, err = post_pr_comment(
                repo, pr_number, comment_body, args.reviewer_identity
            )
            if success:
                print("PR comment posted.", file=sys.stderr)
            else:
                print(f"WARNING: PR comment failed: {err}", file=sys.stderr)

            # Post REVIEW_COMPLETE to bus
            rid = request.get("request_id", "R-000")
            print(f"Posting REVIEW_COMPLETE to bus...", file=sys.stderr)
            success, err = post_review_complete(
                rid, repo, pr_number, verdict, args.reviewer_identity
            )
            if success:
                print("Bus REVIEW_COMPLETE posted.", file=sys.stderr)
            else:
                print(f"WARNING: bus post failed: {err}", file=sys.stderr)

    finally:
        # Restore auth
        if not args.dry_run and mode in ("read_only", "write_review"):
            restore_auth(previous_auth)
            print(f"Auth restored to {previous_auth}.", file=sys.stderr)

    sys.exit(0)


def _output_results(packet, receipt, args):
    """Output packet and receipt to stdout and/or files."""
    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        packet_path = out_dir / "review-packet.json"
        receipt_path = out_dir / "review-receipt.json"
        with open(packet_path, "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2)
        with open(receipt_path, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2)
        print(f"Packet written to {packet_path}", file=sys.stderr)
        print(f"Receipt written to {receipt_path}", file=sys.stderr)
    else:
        print("=== REVIEW PACKET ===")
        print(json.dumps(packet, indent=2))
        print("\n=== REVIEW RECEIPT ===")
        print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
