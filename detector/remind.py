"""Follow-through (M4), laptop only: reads private/ledger.json and private/verdicts.json, pushes via ntfy.

  closing   a SHOWN role whose ATS states a closing date gets one reminder ~2 days before it closes,
            after a check that it is still open (closed: no push; unconfirmed: pushed, saying so)
  followup  an APPLIED role with nothing recorded for 5 days gets one nudge to check its status
  weekly    Sunday after 20:00 IST: every SHOWN role from the last 30 days that has not closed
  skills    Sunday after 20:00 IST: the skills the week's screened JDs asked for, by count,
            marking the ones the resume does not mention

Reminders and nudges go out between 09:00 and 22:00 IST, except a closing reminder with under 12 h
left. Each is sent once; the ledger records when. Called by detector.sync after each pass.

  uv run python -m detector.remind --dry-run   # print what is due now, send and record nothing
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from detector import ledger, notify, screen

IST = notify.IST
DAY_START, DAY_END = 9, 22
CLOSE_WARN = timedelta(days=2)
URGENT = timedelta(hours=12)
FOLLOW_UP_AFTER = timedelta(days=5)
WEEKLY_LOOKBACK = timedelta(days=30)
SUNDAY, WEEKLY_HOUR = 6, 20


def when(s: str | None) -> datetime | None:
    """An ATS-stated time. A date without a time means the end of that day, IST."""
    if not s:
        return None
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(hour=23, minute=59, tzinfo=IST)
        s = s.replace("Z", "+00:00")
        if s[-5] in "+-" and s[-3] != ":":  # +0000 -> +00:00
            s = s[:-2] + ":" + s[-2:]
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def still_open(c: httpx.Client, e: dict) -> tuple[bool | None, str]:
    """True open, False closed, None unconfirmed (never guessed)."""
    if e.get("check_url"):
        try:
            r = c.get(e["check_url"])
        except httpx.HTTPError as err:
            return None, f"detail API {type(err).__name__}"
        if r.status_code in (404, 410):
            return False, f"detail API HTTP {r.status_code}"
        if r.status_code >= 400:
            return None, f"detail API HTTP {r.status_code}"
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "items" in body and not body["items"]:  # Oracle: closed
            return False, "detail API lists no open posting"
        return True, "detail API open"
    from detector.run import live
    ok, why = live(c, e.get("url", ""))
    if ok:
        return True, "JD page loads"
    if why.startswith(("HTTP 404", "HTTP 410", "redirected to closed", "page says")):
        return False, f"JD {why}"
    return None, f"JD {why}"


def daytime(now: datetime) -> bool:
    return DAY_START <= now.astimezone(IST).hour < DAY_END


def closing_due(d: dict, now: datetime) -> list[tuple[str, dict, datetime]]:
    out = []
    for k, e in d["roles"].items():
        close = when(e.get("closes_at"))
        if e["status"] != ledger.SHOWN or not close or e.get("reminded_at") or e.get("closed_at"):
            continue
        left = close - now
        if timedelta(0) < left <= CLOSE_WARN and (daytime(now) or left <= URGENT):
            out.append((k, e, close))
    return sorted(out, key=lambda x: x[2])


def followups_due(d: dict, now: datetime) -> list[tuple[str, dict]]:
    if not daytime(now):
        return []
    return [(k, e) for k, e in d["roles"].items()
            if e["status"] == ledger.APPLIED and not e.get("followed_up_at")
            and (when(e.get("marked_at")) or now) <= now - FOLLOW_UP_AFTER]


def weekly_due(d: dict, name: str, now: datetime) -> bool:
    t = now.astimezone(IST)
    return t.weekday() == SUNDAY and t.hour >= WEEKLY_HOUR and d.get("sent", {}).get(name) != t.date().isoformat()


def unresolved(d: dict, now: datetime) -> list[tuple[str, dict]]:
    rows = []
    for k, e in d["roles"].items():
        shown, close = when(e.get("shown_at")), when(e.get("closes_at"))
        if (e["status"] == ledger.SHOWN and not e.get("closed_at") and shown and shown >= now - WEEKLY_LOOKBACK
                and not (close and close <= now)):
            rows.append((k, e))
    return sorted(rows, key=lambda r: (screen.rank(r[1] if r[1].get("verdict") else None), r[1].get("shown_at", "")),
                  reverse=True)


def weekly_text(rows: list[tuple[str, dict]]) -> str:
    lines = []
    for k, e in rows[:25]:
        v = f" · {e['verdict']} {screen.fit_text(e)}" if e.get("verdict") else ""
        closes = f" · closes {notify.ist(e['closes_at'])}" if e.get("closes_at") else ""
        lines.append(f"{e.get('board', k)}: {e.get('title', k)}{v} · shown {notify.ist(e['shown_at'])}{closes}")
    more = f"\n...and {len(rows) - 25} more (uv run python -m detector.ledger list)" if len(rows) > 25 else ""
    return "\n".join(lines) + more


def skills_text(verdicts: dict, resume: str, now: datetime) -> tuple[int, str]:
    """(number of JDs, text): skills asked for in JDs screened in the last 7 days."""
    week = [v for v in verdicts.values() if v.get("skills") and (when(v.get("screened_at")) or now - timedelta(days=99))
            >= now - timedelta(days=7)]
    counts: Counter = Counter()
    spelling: dict[str, Counter] = {}
    for v in week:
        for s in {s.strip() for s in v["skills"]}:
            low = s.lower()
            counts[low] += 1
            spelling.setdefault(low, Counter())[s] += 1
    res = resume.lower()
    lines = [f"{spelling[low].most_common(1)[0][0]}: {n}" + ("" if low in res else " (not on resume)")
             for low, n in counts.most_common(15)]
    return len(week), "\n".join(lines)


def run(now: datetime | None = None, c: httpx.Client | None = None, dry_run: bool = False) -> list[str]:
    """Send whatever is due. Returns the titles pushed (for logs and tests)."""
    if not ledger.enabled() or not ledger.LEDGER_FILE.exists():
        return []
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    d = ledger.load()
    sent: list[str] = []
    changed = False
    own = c is None
    c = c or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 jobwatch"})
    try:
        for k, e, close in closing_due(d, now):
            ok, why = still_open(c, e)
            if ok is False:
                print(f"remind: {k} closed early ({why}), no reminder")
                if not dry_run:
                    e["closed_at"] = stamp
                    changed = True
                continue
            hours = int((close - now).total_seconds() // 3600)
            left = f"{hours} h" if hours < 48 else "2 days"
            v = f"{e['verdict']} {screen.fit_text(e)}. " if e.get("verdict") else ""
            body = (f"Closes {notify.ist(e['closes_at'])} (in about {left}). {v}No Applied/Skip recorded."
                    + ("" if ok else f"\nCould not confirm it is still open: {why}."))
            title = f"Closing soon: {e.get('board')}: {e.get('title', k)}"
            if notify.push(title, body, click=e.get("url"), tags=["hourglass"], priority=4, dry_run=dry_run,
                           actions=ledger.buttons(e.get("board", ""), e.get("id", ""), e.get("apply_url", ""), e.get("url", ""))):
                sent.append(title)
                if not dry_run:
                    e["reminded_at"] = stamp
                    changed = True

        for k, e in followups_due(d, now):
            title = f"Applied 5+ days ago: {e.get('board')}: {e.get('title', k)}"
            body = (f"Applied {notify.ist(e['marked_at'])}; nothing recorded since. Check the application status. "
                    "If it is quiet, ask Claude to draft a short follow-up (sent only after your yes).")
            if notify.push(title, body, click=e.get("url"), tags=["mailbox"], priority=2, dry_run=dry_run):
                sent.append(title)
                if not dry_run:
                    e["followed_up_at"] = stamp
                    changed = True

        today = now.astimezone(IST).date().isoformat()
        if weekly_due(d, "weekly", now):
            rows = unresolved(d, now)
            ok = True
            if rows:
                title = f"jobwatch: {len(rows)} role{'s' if len(rows) != 1 else ''} with no Applied/Skip"
                ok = notify.push(title, weekly_text(rows), tags=["clipboard"], priority=2, dry_run=dry_run)
                if ok:
                    sent.append(title)
            if ok and not dry_run:
                d.setdefault("sent", {})["weekly"] = today
                changed = True
        if weekly_due(d, "skills", now):
            resume = screen.PRIVATE / "resume.md"
            n, text = skills_text(screen.load_verdicts(), resume.read_text(encoding="utf-8") if resume.exists() else "", now)
            ok = True
            if n and text:
                title = f"jobwatch: skills asked in {n} JD{'s' if n != 1 else ''} this week"
                ok = notify.push(title, text, tags=["bar_chart"], priority=2, dry_run=dry_run)
                if ok:
                    sent.append(title)
            if ok and not dry_run:
                d.setdefault("sent", {})["skills"] = today
                changed = True
    finally:
        if own:
            c.close()
    if changed:
        ledger.save(d)
    for t in sent:
        print(f"remind: sent {t!r}")
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from detector.run import load_env
    load_env()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
