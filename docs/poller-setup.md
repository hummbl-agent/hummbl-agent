# Review Request Poller Setup

The poller (`scripts/review_request_poller.py`) scans the coordination bus for
pending `REVIEW_REQUEST` messages and alerts the fleet on state transitions
and bounded escalation intervals.

## Deployment status

| Status | Description |
|---|---|
| **Validated manually** | The poller script compiles, runs, correctly detects pending requests, suppresses duplicates, and produces structured output. Tested with `--dry-run` and `--json` against the live bus. |
| **NOT deployment-authorized** | No recurring scheduled task is authorized. The operator must explicitly approve scheduling before any automated deployment. A Windows Task Scheduler entry was created and immediately disabled per operator instruction. |

The operator's standing rule prohibits creating new scheduled tasks without
explicit authorization. Do not register a scheduled task, launchd agent, or
cron entry without operator approval.

## What it does

1. Reads the coordination bus TSV file (read-only)
2. Finds all `REVIEW_REQUEST` messages
3. Checks for matching `REVIEW_COMPLETE` or `REVIEW` from `hummbl-agent`,
   scoped by exact `request_id` + `repository` + `PR` (all three required)
4. Reports pending requests to stdout (always)
5. Posts a `STATUS` alert to the bus **only** when `--post` is passed AND
   the `review-poller` identity is registered in the bus allowlist

## Default mode: dry-run (read-only)

The poller defaults to dry-run. It scans and reports but does not post to the
bus. This is the safe operating mode until:

1. The operator approves a recurring schedule.
2. The `review-poller` identity is registered in the bus writer's allowlist.

Use `--post` to attempt bus posts (will fail with a clear error until the
identity is registered).

## Alerting behavior (deduplication + backoff)

The poller does NOT alert on every run. It persists state between runs in a
JSON state file (default: `scripts/.poller-state.json`) and alerts only on:

| Trigger | Description |
|---|---|
| **New request** | First detection of a request_id not in state (state transition) |
| **Material change** | `base_sha` or `head_sha` changed since last alert |
| **Escalation interval** | 30 min, 2 hr, 8 hr, 24 hr after the request's bus timestamp, then every 24 hr |
| **Completion** | A previously-pending request is completed (state transition to resolved) |

Unchanged requests within an escalation interval are suppressed. This
prevents bus spam every 5 minutes for the same pending request.

## Completion matching (strict)

A request is considered completed only when a bus message matches on **all
three** of:

1. `request_id` — exact match (e.g. `R-001`)
2. `repository` — exact match (e.g. `hummbl-agent/hummbl-agent`)
3. `PR` — exact match (e.g. `7`)

Completion signals:

- A message containing `REVIEW_COMPLETE` with all three fields
- A `REVIEW` type message from `hummbl-agent` with all three fields

Stale or malformed completion messages (missing any of the three fields, or
repo/PR mismatch) are rejected. The poller does not check GitHub issue
status. If a review is completed via issue closure without a bus message, the
poller will continue to report it as pending until a `REVIEW_COMPLETE` bus
message is posted with matching `request_id`, `repo`, and `pr`.

## Proposed bus identity

The poller proposes the bus identity `review-poller`. This identity is **not
yet registered** in the bus writer's allowlist. Bus posts will fail with a
clear error until the operator approves it.

Do not post automated alerts under the `devin` identity or any other
human/agent identity. The poller must use its own dedicated non-human
identity once approved.

## Scheduling (requires operator authorization)

The following scheduling configurations are provided for reference only. Do
not deploy without explicit operator approval.

### Windows (Task Scheduler)

> **NOTE**: A task named `hummbl-review-poller` was created and immediately
> disabled per operator instruction on 2026-07-23. It must remain disabled
> until the operator explicitly authorizes recurring deployment.

```powershell
# Register the task (REQUIRES OPERATOR APPROVAL)
$action = New-ScheduledTaskAction `
    -Execute "C:\Users\Owner\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" `
    -Argument "C:\Users\Owner\PROJECTS\hummbl-agent-reviewer\scripts\review_request_poller.py --quiet" `
    -WorkingDirectory "C:\Users\Owner\PROJECTS\hummbl-agent-reviewer"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName "hummbl-review-poller" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Poll coordination bus for pending REVIEW_REQUEST messages" `
    -RunLevel Limited
```

To enable the existing disabled task (REQUIRES OPERATOR APPROVAL):
```powershell
Enable-ScheduledTask -TaskName "hummbl-review-poller"
```

### macOS (launchd)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hummbl.review-poller</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>/Users/nodezero/PROJECTS/hummbl-agent-reviewer/scripts/review_request_poller.py</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>StandardOutPath</key>
    <string>/tmp/hummbl-review-poller.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/hummbl-review-poller.err</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
```

### Linux (cron)

```cron
*/5 * * * * /usr/bin/python3 /path/to/hummbl-agent-reviewer/scripts/review_request_poller.py --quiet >> /var/log/hummbl-review-poller.log 2>&1
```

## Options

| Flag | Effect |
|---|---|
| (default) | Dry-run: scan and report only, no bus post |
| `--post` | Attempt to post alerts to the bus (fails until `review-poller` identity is registered) |
| `--bus-path <path>` | Use a custom bus TSV path instead of auto-detecting |
| `--state-path <path>` | Custom state file path (default: `scripts/.poller-state.json`) |
| `--json` | Machine-readable JSON output |
| `--quiet` | Suppress output when no pending requests and no alerts |
| `--reset-state` | Clear state file and re-alert on all pending requests |
| `--identity <name>` | Override bus identity (default: `review-poller`) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Successful execution — pending or not, alerts posted or suppressed |
| 2 | Operational failure — bus file unreadable, state file error |

Pending requests are a successful execution, not a failure. Nonzero exit
codes are reserved for actual operational failures.

## Alert format

When alerts pass dedup and `--post` is used, the poller posts a `STATUS`
message to the bus:

```
REVIEW_REQUEST_ALERT count=1
  R-001 repo=hummbl-agent/hummbl-agent pr=7 reason=new_request posted=2026-07-23T13:45:25Z
```

Other agents reading the bus can match on `REVIEW_REQUEST_ALERT` to detect
that reviews are waiting and pick them up.
