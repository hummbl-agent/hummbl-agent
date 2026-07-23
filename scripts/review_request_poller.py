#!/usr/bin/env python3
"""Review request poller for the hummbl-agent independent reviewer.

Scans the HUMMBL coordination bus for REVIEW_REQUEST messages that have not
been completed, and alerts the fleet on state transitions and bounded
escalation intervals — not on every run.

Bus message format (TSV):
    timestamp_utc    from    to    type    message

The message field is a JSON object with a "c" key containing the content.
REVIEW_REQUEST content is space-delimited key=value pairs:

    REVIEW_REQUEST request_id=R-001 repo=owner/name pr=7
    base_sha=<40hex> head_sha=<40hex> requested_mode=write_review
    author_identity=hummbl-dev requester_model=GLM-5.2-Devin
    issue=<url> pr_url=<url>

Completion is detected by scanning for a later message containing
REVIEW_COMPLETE with the same request_id, repository, AND PR number. All
three fields must match exactly. A REVIEW from hummbl-agent also completes
a request only when request_id, repo, and PR all match.

Alerting behavior:
    - Alert on first detection of a new pending request (state transition).
    - Alert on material change (base_sha or head_sha changed).
    - Alert on escalation intervals: 30 min, 2 hr, 8 hr, 24 hr after the
      request was first posted, then every 24 hr.
    - Do NOT alert on every run for unchanged requests within an escalation
      interval.
    - Alert when a previously-pending request is completed (state transition
      to resolved).

State persistence:
    A JSON state file tracks the last alert timestamp and known request
    fingerprints per request_id. This enables deduplication across runs.
    Default path: <script_dir>/.poller-state.json

Identity:
    The bus identity is "hummbl-agent", registered 2026-07-23 in the
    founder-mode canonical source (agent_identity.py, bus_writer_core.py,
    anvil-bus.py, agent-roster.md). Trust=MEDIUM. The poller defaults to
    dry-run (read-only) mode. Use --post to emit STATUS alerts to the bus.

Usage:
    python review_request_poller.py                    # scan + report (dry-run default)
    python review_request_poller.py --post             # scan + attempt bus alert
    python review_request_poller.py --bus-path <path>  # custom bus TSV path
    python review_request_poller.py --json             # machine-readable output
    python review_request_poller.py --state-path <p>   # custom state file path
    python review_request_poller.py --reset-state      # clear state and re-alert

Exit codes:
    0 — successful execution (pending or not, alert posted or suppressed)
    2 — bus file unreadable, state file error, or other operational failure

Dependencies: Python 3.8+ stdlib only.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEFAULT_BUS_PATHS = [
    # Anvil canonical path
    Path.home() / "PROJECTS" / "founder-mode" / "_state" / "coordination" / "messages.tsv",
    # nodezero remote path (if mounted)
    "/Users/nodezero/founder-mode/founder_mode/_state/coordination/messages.tsv",
]

# Bus identity — registered 2026-07-23 in founder-mode canonical source
# (agent_identity.py CANONICAL_AGENTS, bus_writer_core.py _RESERVED_AGENT_IDS,
# anvil-bus.py PERMITTED_SENDERS, agent-roster.md). Trust=MEDIUM, Status=ACTIVE.
BUS_IDENTITY = "hummbl-agent"

# Escalation intervals after the request's bus timestamp (not after first alert).
# Alert at: 0 min (first detection), 30 min, 2 hr, 8 hr, 24 hr, then every 24 hr.
ESCALATION_INTERVALS = [
    timedelta(minutes=0),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=8),
    timedelta(hours=24),
]
# After the last explicit interval, repeat every 24 hours.
REPEAT_INTERVAL = timedelta(hours=24)

REQUEST_ID_RE = re.compile(r"request_id=(R-\d{3})")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REVIEW_REQUEST_MARKER = "REVIEW_REQUEST"
REVIEW_COMPLETE_MARKER = "REVIEW_COMPLETE"
REVIEW_MARKER = "REVIEW"

DEFAULT_STATE_PATH = Path(__file__).resolve().parent / ".poller-state.json"


# ---------------------------------------------------------------------------
# Bus file location
# ---------------------------------------------------------------------------

def find_bus_file(custom_path=None):
    """Locate the coordination bus TSV file."""
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return p
        return None
    for p in DEFAULT_BUS_PATHS:
        try:
            if Path(p).is_file():
                return Path(p)
        except (OSError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Bus parsing
# ---------------------------------------------------------------------------

def parse_bus_line(line):
    """Parse a single TSV bus line into a dict.

    Returns None for comments, headers, or malformed lines.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split("\t")
    if len(parts) < 5:
        return None
    entry = {
        "timestamp": parts[0],
        "from": parts[1],
        "to": parts[2],
        "type": parts[3],
        "raw_message": parts[4],
    }
    try:
        msg_obj = json.loads(parts[4])
        entry["message"] = msg_obj.get("c", "")
    except (json.JSONDecodeError, KeyError):
        entry["message"] = parts[4]
    return entry


def extract_field(message, field_name):
    """Extract a key=value field from a space-delimited message string."""
    pattern = re.compile(rf"{field_name}=([^\s]+)")
    match = pattern.search(message)
    return match.group(1) if match else None


def parse_review_request(message):
    """Parse a REVIEW_REQUEST message into structured fields.

    Returns None if the message is not a valid review request.
    A valid request requires at minimum: request_id, repo, pr.
    """
    if REVIEW_REQUEST_MARKER not in message:
        return None
    request_id = extract_field(message, "request_id")
    repo = extract_field(message, "repo")
    pr = extract_field(message, "pr")
    if not request_id or not repo or not pr:
        return None
    return {
        "request_id": request_id,
        "repo": repo,
        "pr": pr,
        "base_sha": extract_field(message, "base_sha"),
        "head_sha": extract_field(message, "head_sha"),
        "requested_mode": extract_field(message, "requested_mode"),
        "author_identity": extract_field(message, "author_identity"),
        "requester_model": extract_field(message, "requester_model"),
        "issue": extract_field(message, "issue"),
        "pr_url": extract_field(message, "pr_url"),
    }


def parse_completion(message, entry_from, entry_type):
    """Parse a completion message (REVIEW_COMPLETE or REVIEW from hummbl-agent).

    Returns a dict with request_id, repo, pr or None if the message is not
    a valid completion signal. All three fields are required for a match.
    """
    # REVIEW_COMPLETE: must contain request_id, repo, and pr
    if REVIEW_COMPLETE_MARKER in message:
        rid = extract_field(message, "request_id")
        repo = extract_field(message, "repo")
        pr = extract_field(message, "pr")
        if rid and repo and pr:
            return {"request_id": rid, "repo": repo, "pr": pr}
        return None

    # REVIEW from hummbl-agent: must contain request_id, repo, and pr
    if entry_from == "hummbl-agent" and entry_type == "REVIEW":
        rid = extract_field(message, "request_id")
        repo = extract_field(message, "repo")
        pr = extract_field(message, "pr")
        if rid and repo and pr:
            return {"request_id": rid, "repo": repo, "pr": pr}
        return None

    return None


# ---------------------------------------------------------------------------
# Bus scanning
# ---------------------------------------------------------------------------

def scan_bus(bus_path):
    """Scan the bus for review requests and completions.

    Returns (requests, completions) where:
        requests: dict of request_id -> request fields (with bus metadata)
        completions: dict of request_id -> completion record (with repo, pr,
                     timestamp, and source for auditability)

    A completion matches a request only when request_id, repo, AND pr all
    match exactly. Stale or malformed completion messages (missing any of
    the three fields) are rejected.
    """
    requests = {}
    completions = {}

    try:
        with open(bus_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                entry = parse_bus_line(line)
                if entry is None:
                    continue

                msg = entry["message"]

                # Detect review requests
                if REVIEW_REQUEST_MARKER in msg:
                    req = parse_review_request(msg)
                    if req and req["request_id"]:
                        req["bus_timestamp"] = entry["timestamp"]
                        req["bus_from"] = entry["from"]
                        # Later requests with the same ID override earlier ones
                        # (re-posting with updated SHAs is a material change)
                        requests[req["request_id"]] = req

                # Detect completions — strict matching on request_id + repo + pr
                comp = parse_completion(msg, entry["from"], entry["type"])
                if comp:
                    rid = comp["request_id"]
                    # Only accept completion if we've seen a matching request
                    # AND repo + pr match exactly
                    if rid in requests:
                        req = requests[rid]
                        if req.get("repo") == comp["repo"] and req.get("pr") == comp["pr"]:
                            completions[rid] = {
                                "request_id": rid,
                                "repo": comp["repo"],
                                "pr": comp["pr"],
                                "completed_at": entry["timestamp"],
                                "completed_by": entry["from"],
                                "completion_type": "REVIEW_COMPLETE" if REVIEW_COMPLETE_MARKER in msg else "REVIEW",
                            }
                        # If repo or pr don't match, the completion is stale or
                        # malformed — reject it silently (do not add to completions)

    except (OSError, IOError) as e:
        print(f"ERROR: cannot read bus file {bus_path}: {e}", file=sys.stderr)
        return None, None

    return requests, completions


def get_pending_requests(bus_path):
    """Return (pending, completions) — pending is a list, completions is a dict.

    pending: list of request dicts that have no matching completion, sorted by
             bus_timestamp.
    completions: dict of request_id -> completion record for requests that
                 were completed (useful for state-transition alerts).
    """
    requests, completions = scan_bus(bus_path)
    if requests is None:
        return None, None
    pending = []
    for rid, req in requests.items():
        if rid not in completions:
            pending.append(req)
    pending.sort(key=lambda r: r.get("bus_timestamp", ""))
    return pending, completions


# ---------------------------------------------------------------------------
# State persistence (deduplication + backoff)
# ---------------------------------------------------------------------------

def load_state(state_path):
    """Load the poller state file.

    Returns a dict with:
        "requests": { request_id: { "fingerprint": str, "last_alerted_at": str,
                                     "first_seen_at": str, "alert_count": int } }
        "completed": { request_id: { "completed_at": str, "alerted_at": str } }
    """
    if not state_path or not Path(state_path).is_file():
        return {"requests": {}, "completed": {}}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
            if not isinstance(state, dict):
                return {"requests": {}, "completed": {}}
            state.setdefault("requests", {})
            state.setdefault("completed", {})
            return state
    except (json.JSONDecodeError, OSError, IOError):
        return {"requests": {}, "completed": {}}


def save_state(state_path, state):
    """Save the poller state file."""
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return True
    except (OSError, IOError) as e:
        print(f"ERROR: cannot write state file {state_path}: {e}", file=sys.stderr)
        return False


def request_fingerprint(req):
    """Compute a fingerprint for material-change detection.

    Changes to base_sha or head_sha constitute a material change.
    """
    return f"{req.get('request_id', '')}:{req.get('base_sha', '')}:{req.get('head_sha', '')}"


def parse_timestamp(ts_str):
    """Parse a bus timestamp string into a datetime object. Returns None on failure."""
    if not ts_str:
        return None
    try:
        # Bus timestamps are ISO 8601 with Z suffix
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def should_alert(req, state_entry, now):
    """Determine whether a pending request should trigger an alert.

    Alert on:
    1. First detection (no state entry) — state transition.
    2. Material change (fingerprint differs) — state transition.
    3. Escalation interval elapsed since the request's bus_timestamp.

    Returns (should_alert: bool, reason: str).
    """
    if state_entry is None:
        return True, "new_request"

    fp = request_fingerprint(req)
    if state_entry.get("fingerprint") != fp:
        return True, "material_change"

    # Check escalation intervals based on the request's bus timestamp
    req_time = parse_timestamp(req.get("bus_timestamp"))
    if req_time is None:
        # Can't parse timestamp — alert as a fallback
        return True, "unparseable_timestamp"

    elapsed = now - req_time
    last_alerted = parse_timestamp(state_entry.get("last_alerted_at"))
    if last_alerted is None:
        return True, "no_prior_alert_record"

    # Find which escalation interval we're in
    alert_times = [req_time + interval for interval in ESCALATION_INTERVALS]
    # Add repeating 24h intervals after the last explicit one
    if elapsed > ESCALATION_INTERVALS[-1]:
        last_explicit = ESCALATION_INTERVALS[-1]
        cycles_since = int((elapsed - last_explicit) / REPEAT_INTERVAL)
        next_repeat = req_time + last_explicit + (cycles_since + 1) * REPEAT_INTERVAL
        alert_times.append(next_repeat - REPEAT_INTERVAL)

    # Find the most recent escalation point that has passed
    due_escalation = None
    for at in alert_times:
        if now >= at and (last_alerted < at):
            due_escalation = at
            break

    if due_escalation is not None:
        elapsed_min = int(elapsed.total_seconds() / 60)
        return True, f"escalation_interval ({elapsed_min}min since posted)"

    return False, "suppressed_no_change"


def should_alert_completion(rid, completions, state):
    """Determine whether a newly-completed request should trigger an alert.

    Alert only if we haven't already alerted about this completion.
    """
    if rid not in completions:
        return False, "not_completed"
    completed_state = state.get("completed", {}).get(rid)
    if completed_state is None:
        return True, "completion_transition"
    return False, "already_alerted_completion"


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_report(pending, completions, alerts, suppressed):
    """Format a human-readable report."""
    lines = []
    lines.append(f"REVIEW_REQUEST_POLL scan={datetime.now(timezone.utc).isoformat()}")
    lines.append(f"  pending={len(pending)} completed={len(completions)} "
                 f"alerts={len(alerts)} suppressed={len(suppressed)}")
    if pending:
        lines.append("")
        lines.append("Pending requests:")
        for req in pending:
            lines.append(
                f"  {req['request_id']} repo={req.get('repo', '?')} "
                f"pr={req.get('pr', '?')} mode={req.get('requested_mode', '?')} "
                f"author={req.get('author_identity', '?')} "
                f"model={req.get('requester_model', '?')} "
                f"posted={req.get('bus_timestamp', '?')}"
            )
    if alerts:
        lines.append("")
        lines.append("Alerts to post:")
        for a in alerts:
            lines.append(f"  {a['request_id']} reason={a['reason']}")
    if suppressed:
        lines.append("")
        lines.append("Suppressed (dedup/backoff):")
        for s in suppressed:
            lines.append(f"  {s['request_id']} reason={s['reason']}")
    return "\n".join(lines)


def format_json_output(pending, completions, alerts, suppressed):
    """Format as JSON."""
    return json.dumps(
        {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "pending_count": len(pending),
            "completed_count": len(completions),
            "alerts": alerts,
            "suppressed": suppressed,
            "pending": pending,
        },
        indent=2,
        default=str,
    )


def format_alert_message(alerts):
    """Format the bus alert message for alerts that passed dedup."""
    lines = [f"REVIEW_REQUEST_ALERT count={len(alerts)}"]
    for a in alerts:
        req = a["request"]
        lines.append(
            f"  {req['request_id']} repo={req.get('repo', '?')} "
            f"pr={req.get('pr', '?')} reason={a['reason']} "
            f"posted={req.get('bus_timestamp', '?')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bus posting
# ---------------------------------------------------------------------------

def post_alert(alerts, identity=BUS_IDENTITY):
    """Post a STATUS alert to the bus via the bus-global wrapper.

    Returns True on success, False on failure.
    """
    try:
        import subprocess

        alert_msg = format_alert_message(alerts)
        result = subprocess.run(
            [
                sys.executable,
                str(Path.home() / "bin" / "bus-global.py"),
                "post",
                identity,
                "all",
                "STATUS",
                alert_msg,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, None
        else:
            stderr = result.stderr.strip()
            return False, stderr
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Poll for pending REVIEW_REQUEST messages on the coordination bus."
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Post STATUS alerts to the bus as 'hummbl-agent'. Default is dry-run (read-only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Scan and report only; do not post alerts to the bus. This is the default.",
    )
    parser.add_argument(
        "--bus-path",
        type=str,
        default=None,
        help="Path to the coordination bus TSV file.",
    )
    parser.add_argument(
        "--state-path",
        type=str,
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to the poller state file (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output when no pending requests and no alerts.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Clear state file and re-alert on all pending requests.",
    )
    parser.add_argument(
        "--identity",
        type=str,
        default=BUS_IDENTITY,
        help=f"Bus identity to use when posting (default: {BUS_IDENTITY}).",
    )
    args = parser.parse_args()

    # --post overrides --dry-run default
    post_mode = args.post and not args.dry_run if args.dry_run else args.post
    # Actually: --dry-run is default True, so we need to handle this properly.
    # If --post is given, we post. Otherwise dry-run.
    post_mode = args.post

    bus_path = find_bus_file(args.bus_path)
    if bus_path is None:
        print("ERROR: could not locate coordination bus TSV file", file=sys.stderr)
        print("Tried:", file=sys.stderr)
        for p in DEFAULT_BUS_PATHS:
            print(f"  {p}", file=sys.stderr)
        if args.bus_path:
            print(f"  {args.bus_path}", file=sys.stderr)
        sys.exit(2)

    pending, completions = get_pending_requests(bus_path)
    if pending is None:
        print(f"ERROR: failed to scan bus at {bus_path}", file=sys.stderr)
        sys.exit(2)

    # Load or reset state
    if args.reset_state:
        state = {"requests": {}, "completed": {}}
    else:
        state = load_state(args.state_path)

    now = datetime.now(timezone.utc)
    alerts = []
    suppressed = []

    # Evaluate each pending request for alerting
    for req in pending:
        rid = req["request_id"]
        state_entry = state.get("requests", {}).get(rid)
        should, reason = should_alert(req, state_entry, now)
        if should:
            alerts.append({"request_id": rid, "reason": reason, "request": req})
        else:
            suppressed.append({"request_id": rid, "reason": reason})

    # Evaluate completions for transition alerts
    for rid, comp in completions.items():
        should, reason = should_alert_completion(rid, completions, state)
        if should:
            alerts.append({
                "request_id": rid,
                "reason": reason,
                "completion": comp,
            })

    # Report
    has_output = pending or alerts or suppressed
    if has_output or not args.quiet:
        if args.json:
            print(format_json_output(pending, completions, alerts, suppressed))
        else:
            print(format_report(pending, completions, alerts, suppressed))

    # Post alerts if in post mode
    post_result = None
    if post_mode and alerts:
        success, err = post_alert(alerts, identity=args.identity)
        if success:
            post_result = "posted"
            if not args.json:
                print(f"\nAlert posted to bus as '{args.identity}' for {len(alerts)} alert(s).")
        else:
            post_result = f"failed: {err}"
            print(f"\nWARNING: bus post failed: {err}", file=sys.stderr)
            print(f"The identity '{args.identity}' may not be registered in the bus "
                  f"allowlist. Use --dry-run until approved.", file=sys.stderr)
    elif post_mode and not alerts:
        post_result = "no_alerts"
        if not args.quiet and not args.json:
            print("\nNo alerts to post (all suppressed by dedup/backoff).")
    elif not post_mode and alerts:
        if not args.json:
            print(f"\nDry-run: {len(alerts)} alert(s) would be posted. Use --post to attempt bus post.")
        post_result = "dry_run"

    # Update state
    for a in alerts:
        rid = a["request_id"]
        if "request" in a:
            req = a["request"]
            state.setdefault("requests", {})[rid] = {
                "fingerprint": request_fingerprint(req),
                "last_alerted_at": now.isoformat(),
                "first_seen_at": state.get("requests", {}).get(rid, {}).get(
                    "first_seen_at", now.isoformat()
                ),
                "alert_count": state.get("requests", {}).get(rid, {}).get("alert_count", 0) + 1,
            }
        if "completion" in a:
            comp = a["completion"]
            state.setdefault("completed", {})[rid] = {
                "completed_at": comp.get("completed_at"),
                "alerted_at": now.isoformat(),
            }

    # Also update state for suppressed requests (keep first_seen_at current)
    for s in suppressed:
        rid = s["request_id"]
        existing = state.get("requests", {}).get(rid)
        if existing and "first_seen_at" not in existing:
            existing["first_seen_at"] = now.isoformat()

    # Save state
    if not save_state(args.state_path, state):
        # State save failure is a warning, not a fatal error
        print(f"WARNING: could not save state to {args.state_path}", file=sys.stderr)

    # Exit 0 for successful execution regardless of pending/alert state.
    # Exit 2 only for operational failures (bus unreadable, etc. — handled above).
    sys.exit(0)


if __name__ == "__main__":
    main()
