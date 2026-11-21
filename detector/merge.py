"""Merge two copies of state/ written by different runners (laptop and Actions).

Both runners commit state/seen.json. When they record new IDs at the same time, a plain git merge
conflicts. This merges by meaning instead: union of IDs, the further-along status wins, the earliest
first_seen is kept. No ID is ever lost, so a posting one runner alerted is never "new" to the other.

Used as a git merge driver (registered by detector.sync / the workflow, declared in .gitattributes):
  python -m detector.merge %O %A %B      # writes the merged result to %A, exit 0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Higher wins. Terminal states beat in-progress ones; "alerted" beats all so nothing re-alerts.
RANK = {"pending": 1, "baseline": 2, "filtered": 3, "dead": 4, "alerted": 5}


def _job(a: dict, b: dict) -> dict:
    win, lose = (a, b) if RANK.get(a.get("status"), 0) >= RANK.get(b.get("status"), 0) else (b, a)
    out = {**lose, **win}
    out["first_seen"] = min(x["first_seen"] for x in (a, b) if x.get("first_seen"))
    if out.get("status") == "pending":
        out["tries"] = max(a.get("tries", 0), b.get("tries", 0))
    return out


def merge_seen(a: dict, b: dict) -> dict:
    boards: dict = {}
    for name in sorted(set(a.get("boards", {})) | set(b.get("boards", {}))):
        x, y = a["boards"].get(name, {}), b["boards"].get(name, {})
        jobs = dict(x.get("jobs", {}))
        for jid, entry in y.get("jobs", {}).items():
            jobs[jid] = _job(jobs[jid], entry) if jid in jobs else entry
        board = {"jobs": jobs}
        stamps = [s["baseline_at"] for s in (x, y) if "baseline_at" in s]
        if stamps:
            board["baseline_at"] = min(stamps)
        boards[name] = board
    return {"version": max(a.get("version", 1), b.get("version", 1)), "boards": boards}


def merge_health(a: dict, b: dict) -> dict:
    # Per board, keep the copy reporting more consecutive failures: an outage stays visible.
    return {n: max((a.get(n), b.get(n)), key=lambda h: (h or {}).get("fails", -1)) for n in set(a) | set(b)}


def main(argv: list[str]) -> int:
    _base, ours, theirs = (Path(p) for p in argv[:3])
    a, b = (json.loads(p.read_text(encoding="utf-8")) for p in (ours, theirs))
    merged = merge_seen(a, b) if "boards" in a or "boards" in b else merge_health(a, b)
    ours.write_text(json.dumps(merged, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
