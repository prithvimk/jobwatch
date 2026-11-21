"""Committed state: IDs and timestamps only (safe for a public repo).

state/seen.json  = union of every job ID ever seen, per board, with first_seen and a status:
    baseline  present when the board was first tracked (never alerted)
    filtered  failed the prefilter
    pending   passed the prefilter but liveness failed; retried up to max_liveness_tries
    alerted   pushed to the phone
    screened  judged OUT by the screener: not pushed, listed in the daily digest
    dead      never became live
    repost    same requisition as a role already applied to or skipped (private ledger): not pushed
state/health.json = per board: ok, consecutive failures, last error. Changes only on failure/recovery.
IDs are never removed, so a repost under the same ID is never "new".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("JOBWATCH_STATE_DIR", Path(__file__).resolve().parent.parent / "state"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(name: str, default: dict) -> dict:
    p = STATE_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _save(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f".{name}.tmp"
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_DIR / name)


def load_seen() -> dict:
    return _load("seen.json", {"version": 1, "boards": {}})


def save_seen(seen: dict) -> None:
    _save("seen.json", seen)


def load_health() -> dict:
    return _load("health.json", {})


def save_health(health: dict) -> None:
    _save("health.json", health)
