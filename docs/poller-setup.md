# Review Request Poller Setup

The poller (`scripts/review_request_poller.py`) scans the coordination bus for
pending `REVIEW_REQUEST` messages and alerts the fleet. It should run on a
schedule so requests are picked up without manual intervention.

## What it does

1. Reads the coordination bus TSV file
2. Finds all `REVIEW_REQUEST` messages
3. Checks for matching `REVIEW_COMPLETE` or a `REVIEW` from `hummbl-agent`
4. Reports pending requests to stdout
5. Posts a `STATUS` alert to the bus (unless `--dry-run`)

## Scheduling

### Windows (Task Scheduler)

Create a scheduled task that runs every 5 minutes:

```powershell
# Register the task
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "C:\Users\Owner\PROJECTS\hummbl-agent-reviewer\scripts\review_request_poller.py" `
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

Verify it registered:
```powershell
Get-ScheduledTask -TaskName "hummbl-review-poller" | Format-List
```

### macOS (launchd)

Create `~/Library/LaunchAgents/com.hummbl.review-poller.plist`:

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

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.hummbl.review-poller.plist
```

### Linux (cron)

```cron
*/5 * * * * /usr/bin/python3 /path/to/hummbl-agent-reviewer/scripts/review_request_poller.py --quiet >> /var/log/hummbl-review-poller.log 2>&1
```

## Options

| Flag | Effect |
|---|---|
| `--dry-run` | Scan and report only; do not post alerts to the bus |
| `--bus-path <path>` | Use a custom bus TSV path instead of auto-detecting |
| `--json` | Machine-readable JSON output |
| `--quiet` | Suppress output when no pending requests |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No pending requests |
| 1 | Pending requests found (and alert posted unless --dry-run) |
| 2 | Bus file unreadable or malformed |

## Alert format

When pending requests are found, the poller posts a `STATUS` message to the
bus:

```
PENDING_REVIEW_REQUESTS count=1
  R-001 repo=hummbl-agent/hummbl-agent pr=7 mode=write_review
  author=hummbl-dev model=GLM-5.2-Devin posted=2026-07-23T13:45:25Z
```

Other agents reading the bus can match on `PENDING_REVIEW_REQUESTS` to detect
that reviews are waiting and pick them up.

## Completion detection

A request is considered completed when any of these appear on the bus:

1. A message containing `REVIEW_COMPLETE` with the same `request_id`
2. A `REVIEW` type message from `hummbl-agent` referencing the same `repo`
   and `pr`

The poller does not check GitHub issue status. If a review is completed via
issue closure without a bus message, the poller will continue to report it as
pending until a `REVIEW_COMPLETE` bus message is posted.
