#!/usr/bin/env python3
"""Review request poller for the hummbl-agent independent reviewer.

Scans the HUMMBL coordination bus for REVIEW_REQUEST messages that have not
been completed, and alerts the fleet. Designed to run on a schedule (cron,
launchd, or Windows Task Scheduler).

Bus message format (TSV):
    timestamp_utc    from    to    type    message

The message field is a JSON object with a "c" key containing the content.
REVIEW_REQUEST content is space-delimited key=value pairs:

    REVIEW_REQUEST request_id=R-001 repo=owner/name pr=7
    base_sha=<40hex> head_sha=<40hex> requested_mode=write_review
    author_identity=hummbl-dev requester_model=GLM-5.2-Devin
    issue=<url> pr_url=<url>

Completion is detected by scanning for a later message containing
REVIEW_COMPLETE with the same request_id, or a REVIEW message from
hummbl-agent referencing the same repo and PR.

Usage:
    python review_request_poller.py                    # scan + alert
    python review_request_poller.py --dry-run          # scan only, no bus post
    python review_request_poller.py --bus-path <path>  # custom bus TSV path
    python review_request_poller.py --json             # machine-readable output

Exit codes:
    0 — no pending requests (or dry-run with no pending)
    1 — pending requests found and alert posted
    2 — bus file unreadable or malformed

Dependencies: Python 3.8+ stdlib only.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BUS_PATHS = [
    # Anvil canonical path
    Path.home() / "PROJECTS" / "founder-mode" / "_state" / "coordination" / "messages.tsv",
    # nodezero remote path (if mounted)
    "/Users/nodezero/founder-mode/founder_mode/_state/coordination/messages.tsv",
]

REQUEST_ID_RE = re.compile(r"request_id=(R-\d{3})")
REVIEW_REQUEST_MARKER = "REVIEW_REQUEST"
REVIEW_COMPLETE_MARKER = "REVIEW_COMPLETE"
REVIEW_MARKER = "REVIEW"


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
    # Try to parse the message as JSON with a "c" field
    try:
        msg_obj = json.loads(parts[4])
        entry["message"] = msg_obj.get("c", "")
    except (json.JSONDecodeError, KeyError):
        # Fallback: treat the raw message as the content
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
    """
    if REVIEW_REQUEST_MARKER not in message:
        return None
    request_id = extract_field(message, "request_id")
    if not request_id:
        return None
    return {
        "request_id": request_id,
        "repo": extract_field(message, "repo"),
        "pr": extract_field(message, "pr"),
        "base_sha": extract_field(message, "base_sha"),
        "head_sha": extract_field(message, "head_sha"),
        "requested_mode": extract_field(message, "requested_mode"),
        "author_identity": extract_field(message, "author_identity"),
        "requester_model": extract_field(message, "requester_model"),
        "issue": extract_field(message, "issue"),
        "pr_url": extract_field(message, "pr_url"),
    }


def scan_bus(bus_path):
    """Scan the bus for review requests and completions.

    Returns (requests, completions) where:
        requests: dict of request_id -> {fields, bus_entry}
        completions: set of request_ids that have been completed
    """
    requests = {}
    completions = set()

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
                        requests[req["request_id"]] = req

                # Detect completions — REVIEW_COMPLETE with matching request_id
                if REVIEW_COMPLETE_MARKER in msg:
                    rid = extract_field(msg, "request_id")
                    if rid:
                        completions.add(rid)

                # Also detect a REVIEW from hummbl-agent referencing the same
                # repo+pr as a completion signal (the reviewer posted a receipt)
                if entry["from"] == "hummbl-agent" and entry["type"] == "REVIEW":
                    repo = extract_field(msg, "repo")
                    pr = extract_field(msg, "pr")
                    if repo and pr:
                        for rid, req in requests.items():
                            if req.get("repo") == repo and req.get("pr") == pr:
                                completions.add(rid)

    except (OSError, IOError) as e:
        print(f"ERROR: cannot read bus file {bus_path}: {e}", file=sys.stderr)
        return None, None

    return requests, completions


def get_pending_requests(bus_path):
    """Return list of pending (uncompleted) review requests."""
    requests, completions = scan_bus(bus_path)
    if requests is None:
        return None
    pending = []
    for rid, req in requests.items():
        if rid not in completions:
            pending.append(req)
    # Sort by timestamp
    pending.sort(key=lambda r: r.get("bus_timestamp", ""))
    return pending


def format_alert(pending):
    """Format pending requests into a human-readable alert string."""
    lines = [f"PENDING_REVIEW_REQUESTS count={len(pending)}"]
    for req in pending:
        lines.append(
            f"  {req['request_id']} repo={req.get('repo', '?')} "
            f"pr={req.get('pr', '?')} mode={req.get('requested_mode', '?')} "
            f"author={req.get('author_identity', '?')} "
            f"model={req.get('requester_model', '?')} "
            f"posted={req.get('bus_timestamp', '?')}"
        )
    return "\n".join(lines)


def format_json(pending):
    """Format pending requests as JSON."""
    return json.dumps(
        {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "pending_count": len(pending),
            "pending": pending,
        },
        indent=2,
    )


def post_alert(pending):
    """Post a STATUS alert to the bus via the bus-global wrapper.

    Returns True on success, False on failure.
    """
    try:
        import subprocess

        alert_msg = format_alert(pending)
        result = subprocess.run(
            [
                sys.executable,
                str(Path.home() / "bin" / "bus-global.py"),
                "post",
                "devin",
                "all",
                "STATUS",
                alert_msg,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        else:
            print(f"ERROR: bus post failed: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"ERROR: cannot post to bus: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Poll for pending REVIEW_REQUEST messages on the coordination bus."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report only; do not post alerts to the bus.",
    )
    parser.add_argument(
        "--bus-path",
        type=str,
        default=None,
        help="Path to the coordination bus TSV file.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output when no pending requests.",
    )
    args = parser.parse_args()

    bus_path = find_bus_file(args.bus_path)
    if bus_path is None:
        print("ERROR: could not locate coordination bus TSV file", file=sys.stderr)
        print("Tried:", file=sys.stderr)
        for p in DEFAULT_BUS_PATHS:
            print(f"  {p}", file=sys.stderr)
        if args.bus_path:
            print(f"  {args.bus_path}", file=sys.stderr)
        sys.exit(2)

    pending = get_pending_requests(bus_path)
    if pending is None:
        print(f"ERROR: failed to scan bus at {bus_path}", file=sys.stderr)
        sys.exit(2)

    if not pending:
        if not args.quiet:
            print("No pending review requests.")
        sys.exit(0)

    # Report
    if args.json:
        print(format_json(pending))
    else:
        print(format_alert(pending))

    # Alert
    if not args.dry_run:
        if post_alert(pending):
            print(f"Alert posted to bus for {len(pending)} pending request(s).")
        else:
            print("WARNING: failed to post alert to bus.", file=sys.stderr)

    sys.exit(1)


if __name__ == "__main__":
    main()
