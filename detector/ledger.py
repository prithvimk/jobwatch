"""Ledger (M4): what happened to every role alerted to the phone. Private: private/ledger.json, never committed.

One entry per posting, keyed "<board>:<job id>" (the same key as private/verdicts.json), carrying the
employer requisition id when the ATS exposes one. States:
    SHOWN    pushed to the phone, no reaction yet
    APPLIED  the user applied
    SKIPPED  the user decided not to
APPLIED and SKIPPED are never recommended again: a new posting on the same board with the same
requisition id (a repost under a new job id) is suppressed. A repost of a SHOWN role is alerted again
with a note saying when it was first shown.

Marking a role:
  - phone: the alert's "Applied" / "Skip" buttons POST "<status> <key>" to the reply topic
    (NTFY_REPLY_TOPIC, default "<NTFY_TOPIC>-reply"). The laptop reads it each pass (ntfy.sh keeps
    messages 12 h, so a laptop that is off longer loses those taps; mark them with the CLI).
  - laptop:
      uv run python -m detector.ledger list            # unresolved (SHOWN) roles, newest first
      uv run python -m detector.ledger list --all
      uv run python -m detector.ledger applied <ref>   # ref: board:id, job id, or requisition id
      uv run python -m detector.ledger skipped <ref>
      uv run python -m detector.ledger shown <ref>     # undo a mark
      uv run python -m detector.ledger pull            # read phone taps now
Actions has no private/ directory, so it neither reads nor writes the ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from detector import state
from detector.sources import Job

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "private"
LEDGER_FILE = PRIVATE / "ledger.json"
SHOWN, APPLIED, SKIPPED = "SHOWN", "APPLIED", "SKIPPED"
STATUSES = (SHOWN, APPLIED, SKIPPED)
RESOLVED = (APPLIED, SKIPPED)


def enabled() -> bool:
    return PRIVATE.is_dir()


def key(board: str, job_id: str) -> str:
    return f"{board}:{job_id}"


def load() -> dict:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return {"version": 1, "roles": {}, "reply_since": ""}


def save(d: dict) -> None:
    PRIVATE.mkdir(exist_ok=True)
    tmp = LEDGER_FILE.with_name(".ledger.json.tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(LEDGER_FILE)


def record_shown(d: dict, job: Job, verdict: dict | None = None, at: str | None = None) -> dict:
    """Called after a successful push. Never downgrades an APPLIED/SKIPPED mark."""
    e = d["roles"].setdefault(key(job.board, job.id), {"status": SHOWN})
    e.update(board=job.board, id=job.id, title=job.title, url=job.url, apply_url=job.apply_url)
    e.setdefault("shown_at", at or state.now_iso())
    if job.check_urls:
        e["check_url"] = job.check_urls[0]  # ATS detail API: decides whether it is still open (reminders)
    for k, v in (("req", job.req_id), ("closes_at", job.closes_at), ("posted_at", job.posted_at)):
        if v:
            e[k] = v
    if verdict:
        e.update(verdict=verdict["verdict"], fit=verdict["fit"])
    return e


def same_req(d: dict, job: Job) -> dict | None:
    """An earlier ledger entry for the same requisition on the same board under a different job id."""
    if not job.req_id:
        return None
    hits = [e for k, e in d["roles"].items()
            if e.get("board") == job.board and e.get("req") == job.req_id and k != key(job.board, job.id)]
    hits.sort(key=lambda e: e["status"] in RESOLVED, reverse=True)  # a resolved mark wins
    return hits[0] if hits else None


def mark(d: dict, k: str, status: str, at: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    board, _, jid = k.rpartition(":")
    e = d["roles"].setdefault(k, {"board": board, "id": jid, "status": SHOWN})
    e["status"] = status
    e["marked_at"] = at or state.now_iso()
    if status == SHOWN:
        e.pop("marked_at")
    return e


def find(d: dict, ref: str, seen: dict | None = None) -> list[str]:
    """Resolve a CLI ref: exact key, else a job id or requisition id in the ledger, else a job id in state."""
    r = ref.strip()
    if r in d["roles"]:
        return [r]
    low = r.lower()
    hits = [k for k, e in d["roles"].items() if k.lower() == low or str(e.get("id", "")).lower() == low
            or str(e.get("req", "")).lower() == low]
    if hits:
        return hits
    seen = seen if seen is not None else state.load_seen()
    out = []
    for board, bs in seen.get("boards", {}).items():
        for jid, e in bs.get("jobs", {}).items():
            if key(board, jid).lower() == low or jid.lower() == low or str(e.get("req", "")).lower() == low:
                out.append(key(board, jid))
    return out


# ---------------------------------------------------------------- phone replies via ntfy

def _server() -> str:
    return os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def reply_topic() -> str | None:
    if os.environ.get("NTFY_REPLY_TOPIC"):
        return os.environ["NTFY_REPLY_TOPIC"]
    return f"{os.environ['NTFY_TOPIC']}-reply" if os.environ.get("NTFY_TOPIC") else None


def actions(job: Job) -> list[dict]:
    return buttons(job.board, job.id, job.apply_url, job.url)


def buttons(board: str, job_id: str, apply_url: str, url: str) -> list[dict]:
    """ntfy allows 3 buttons: Apply opens the form; Applied / Skip post to the reply topic.
    (Tapping the notification itself opens the JD.)"""
    acts = [{"action": "view", "label": "Apply", "url": apply_url}]
    topic = reply_topic()
    if not topic:
        return acts + [{"action": "view", "label": "JD", "url": url}]
    headers = {"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}"} if os.environ.get("NTFY_TOKEN") else {}
    for label, status in (("Applied", APPLIED), ("Skip", SKIPPED)):
        a = {"action": "http", "label": label, "url": f"{_server()}/{topic}", "method": "POST",
             "body": f"{status.lower()} {key(board, job_id)}", "clear": True}
        if headers:
            a["headers"] = headers
        acts.append(a)
    return acts


def parse_reply(text: str) -> tuple[str, str] | None:
    verb, _, k = text.strip().partition(" ")
    status = {"applied": APPLIED, "skipped": SKIPPED, "skip": SKIPPED, "shown": SHOWN}.get(verb.lower())
    return (status, k.strip()) if status and ":" in k else None


def fetch_replies(c: httpx.Client, since: str) -> list[dict]:
    topic = reply_topic()
    if not topic:
        return []
    headers = {"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}"} if os.environ.get("NTFY_TOKEN") else {}
    r = c.get(f"{_server()}/{topic}/json", params={"poll": "1", "since": since or "12h"}, headers=headers)
    r.raise_for_status()
    msgs = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    return [m for m in msgs if m.get("event") == "message"]


def apply_replies(d: dict, msgs: list[dict]) -> list[tuple[str, str]]:
    done = []
    for m in sorted(msgs, key=lambda m: m.get("time", 0)):
        p = parse_reply(m.get("message", ""))
        if p:
            status, k = p
            at = datetime.fromtimestamp(m["time"], timezone.utc).isoformat(timespec="seconds") if m.get("time") else None
            mark(d, k, status, at)
            done.append((k, status))
        if m.get("id"):
            d["reply_since"] = m["id"]
    return done


def pull(c: httpx.Client | None = None) -> list[tuple[str, str]]:
    """Read phone taps since the last one read and record them. Errors are printed, never raised."""
    if not enabled() or not reply_topic():
        return []
    d = load()
    try:
        if c is None:
            with httpx.Client(timeout=15) as cc:
                msgs = fetch_replies(cc, d.get("reply_since", ""))
        else:
            msgs = fetch_replies(c, d.get("reply_since", ""))
    except httpx.HTTPError as e:
        print(f"ledger: reading phone replies failed: {type(e).__name__}: {str(e)[:150]}")
        return []
    done = apply_replies(d, msgs)
    if msgs:
        save(d)
    for k, status in done:
        print(f"ledger: {k} -> {status} (phone)")
    return done


# ---------------------------------------------------------------- CLI

def _line(k: str, e: dict) -> str:
    v = f" {e['verdict']} fit {e['fit']}" if e.get("verdict") else ""
    closes = f" closes {e['closes_at'][:10]}" if e.get("closes_at") else ""
    shown = (e.get("shown_at") or "")[:10]
    return f"{e['status']:<8} {shown:<10} {k}  {e.get('title', '(title unknown: alerted from Actions)')}{v}{closes}"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="detector.ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    for name in ("applied", "skipped", "shown"):
        p = sub.add_parser(name)
        p.add_argument("refs", nargs="+")
    sub.add_parser("pull")
    args = ap.parse_args(argv)

    from detector.run import load_env
    load_env()
    if args.cmd == "pull":
        done = pull()
        print(f"{len(done)} mark(s) read from the phone")
        return 0
    d = load()
    if args.cmd == "list":
        rows = [(k, e) for k, e in d["roles"].items() if args.all or e["status"] == SHOWN]
        rows.sort(key=lambda r: r[1].get("shown_at") or "", reverse=True)
        for k, e in rows:
            print(_line(k, e))
        print(f"{len(rows)} role(s)")
        return 0
    rc = 0
    for ref in args.refs:
        hits = find(d, ref)
        if len(hits) != 1:
            print(f"{ref!r}: {'no match' if not hits else 'ambiguous: ' + ', '.join(hits)}")
            rc = 1
            continue
        e = mark(d, hits[0], args.cmd.upper())
        print(_line(hits[0], e))
    save(d)
    return rc


if __name__ == "__main__":
    sys.exit(main())
