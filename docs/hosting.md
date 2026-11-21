# Detector hosting (M1 decision)

**Decision: hybrid. The laptop is primary (Task Scheduler, every 5 min while awake). GitHub Actions on a public repo is the fallback (cron every 10 min), and skips while the laptop has run in the last 15 min.**

## How the hybrid works

- `detector/sync.py` (laptop): pull state, run the detector, commit only `state/`, push, then set the repo variable `JOBWATCH_LOCAL_AT` to now. Installed by `scripts/install_task.ps1` through the venv's `pythonw.exe` (no console window), logging to `private/logs/sync.log`. With no `origin` remote it runs and commits locally only.
- The workflow's first step skips scheduled runs while `JOBWATCH_LOCAL_AT` is under 15 min old. Manual dispatches always run.
- Missed passes lose nothing: every pass diffs against every ID ever seen, so the first pass after the laptop wakes catches what was posted meanwhile (with `first seen` set to that pass).
- Concurrent state commits merge by meaning, not by text: `.gitattributes` routes `state/*.json` to `detector/merge.py` (union of IDs, the further-along status wins, earliest `first_seen` kept). The driver is registered in local git config by `sync.py` and by the workflow.
- Duplicate alerts are possible only in the window between the laptop going to sleep and its heartbeat expiring, and only if both runners see the same new posting before either pushes.

## Inputs (measured 2026-11-21)

- One detector pass over the 3 easy boards (1,111 postings) takes about 4 s. Liveness checks only run for new prefilter hits.
- An Actions job adds about 20-40 s of checkout, uv setup and `uv sync`. Actions bills each job rounded **up** to the whole minute, so **1 billed minute per run**.
- Runs per month at a 10-min cadence: 6/h × 24 × 30 = **4,320**.

## Options

| | Public repo + Actions | Private repo + Actions | Local cron (Task Scheduler) |
|---|---|---|---|
| Cost | $0: standard runners are free and unmetered for public repos | Free plan includes 2,000 min/month | $0 |
| Cadence that fits | 10 min (5 min is GitHub's floor) | 4,320 min/month at 10 min is over the 2,000 limit. 2,000 / 30 / 24 = 2.8 runs/h, so **every 22 min at best**; use every 30 min (1,440 min) to leave room for M2 adapters | 5 min or faster |
| Worst-case detection latency | 10 min + schedule jitter | 30 min + jitter | 5 min, **only while the laptop is awake** |
| Uptime | 24/7 | 24/7 | Lost overnight, asleep, or travelling |
| What's exposed | Code, board list, prefilter keywords, seen IDs and timestamps | Nothing | Nothing |

## Why public Actions

Detection latency is the lever. The laptop gives 5-min passes with no jitter during the hours an application can actually be submitted (Simplify autofill lives there), and Workday has been shown to work from the home IP. Public Actions is the only free option that runs 24/7 at a 10-min cadence, so it covers time away from the laptop. Private Actions has to triple the interval to stay inside free minutes. Local cron is faster but goes dark exactly when the laptop sleeps.

The privacy cost is small by design: `state/` holds only IDs, timestamps and statuses, and `private/` is gitignored. Personal details and the screening spec live in the gitignored `CLAUDE.local.md`.

## Known risks and mitigations

- **Schedule jitter.** GitHub delays scheduled runs under load, worst near the top of the hour, and can drop them. The cron uses minutes offset from :00. If measured latency is bad, an external free scheduler can call `workflow_dispatch` through the API.
- **60-day auto-disable.** GitHub disables schedules in public repos with no activity for 60 days. State commits (any new posting on 26 boards) count as activity.
- **Workday throttling (M2).** Workday answers Python TLS clients with 429, so the adapter uses `curl`, paced 1 s apart. Checked 2026-11-21: all 9 Workday boards fetched cleanly from both the home IP and an Actions runner. A normal pass is about 1 min (one page per board plus one rotating full sweep); the slowest full sweep (Citi, 47 pages) is about 2 min.
- **Overlapping runs.** `concurrency: detector` serializes runs, and the commit step rebases before pushing.
