"""Outcomes: what happened after applying, for judging jobwatch itself. Laptop only; everything lives in private/.

  JD copies  Each alerted role's JD text is saved to private/jds/<board>-<id>.md at alert time (run.py), so
             it stays readable after the posting closes. Roles alerted before that, and any whose save
             failed, are caught up each pass (APPLIED first, a few per pass, `JD_TRIES` attempts, then
             `jd_error` is recorded and the JD stays unknown).
  closures   Each APPLIED role is checked about once a day until it closes: `last_open_at` when open,
             `closed_at` (and `closed_why`) the first time the ATS says it is gone. An unreachable check
             records nothing but `open_check_error`, never a guess.
  events     Outcomes come from a report of the user's inbox (made by Gemini with `prompt`), imported
             with `import`, or one at a time with `add`. Each APPLIED role keeps a dated `events` list:
             acknowledged, assessment, recruiter_screen, interview, offer, rejected, withdrawn.
             A row is matched to a role by requisition id, else by company and title; a row that matches
             no role or several is listed and left out, never guessed.
  report     Reply and progress rates by verdict, fit, requirement coverage, posting age at apply and
             company. "No reply" is computed (nothing but an acknowledgement after NO_REPLY_AFTER).

  uv run python -m detector.outcomes prompt            # print the Gemini prompt (lists the applied roles)
  uv run python -m detector.outcomes import <file>     # CSV or markdown table from Gemini; --dry-run
  uv run python -m detector.outcomes add <ref> <stage> [--at YYYY-MM-DD] [--note TEXT]
  uv run python -m detector.outcomes jds [--limit N]   # save missing JD copies now
  uv run python -m detector.outcomes closed [--force]  # run the closure checks now
  uv run python -m detector.outcomes report            # print and save private/reports/outcomes-<date>.md
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from detector import ledger, state
from detector.sources import Job

STAGES = ("acknowledged", "assessment", "recruiter_screen", "interview", "offer", "rejected", "withdrawn")
PROGRESS = {"acknowledged": 1, "assessment": 2, "recruiter_screen": 3, "interview": 4, "offer": 5}
SYNONYMS = [
    ("withdrawn", r"withdr"),
    ("rejected", r"reject|regret|not (been )?selected|unsuccessful|not moving forward|not (be )?progress|declin|position (has been )?filled|other candidates"),
    ("offer", r"\boffer"),
    ("interview", r"interview|onsite|on-site|panel|technical round|hiring manager"),
    ("recruiter_screen", r"recruiter|phone screen|screening call|screen call|intro(ductory)? call|hr call"),
    ("assessment", r"assessment|\boa\b|online test|coding test|hackerrank|codility|codesignal|take[- ]home|test invit"),
    ("acknowledged", r"acknowledg|received|confirmation|thank you for (applying|your application)|application submitted|applied"),
]
NO_REPLY_AFTER = timedelta(days=21)
CHECK_EVERY = timedelta(hours=20)
JD_TRIES = 3
JD_PER_PASS = 8
# Messages the adapters give for a gone posting (a 404/410, "no such open posting", canApply=false,
# activeOnJobsListing=false, a SuccessFactors page without the requisition, a redirect off the job page).
# Anything else (403, 429, timeouts) is unconfirmed.
CLOSED_RE = re.compile(r"\b(404|410)\b|closed|no such|no posting|no open|no longer|lists no|canApply=false|"
                       r"activeOnJobsListing=false|not active|does not (show|name) (the )?requisition|posting status|"
                       r"page says|redirected to", re.I)
COMPANY_NOISE = re.compile(r"\b(india|pvt|private|ltd|limited|llp|inc|corp(oration)?|co|company|technologies|technology|"
                           r"tech|services|solutions|centre|center|global|group|the|and|of|plc|n\.?a)\b")
ALIASES = {
    "JPMC": ["jpmorgan", "jp morgan", "j p morgan", "jpmorganchase", "chase"],
    "Bank of America": ["bofa", "ba continuum", "bank of america"],
    "Goldman Sachs": ["goldman"],
    "Walmart Global Tech": ["walmart"],
    "D. E. Shaw India": ["d e shaw", "de shaw", "deshaw"],
    "S&P Global": ["s p", "spglobal"],
    "GE HealthCare": ["ge healthcare", "gehc"],
    "Morgan Stanley": ["morgan stanley"],
    "Eightfold AI": ["eightfold"],
    "Lowe's": ["lowes", "lowe s"],
    "P&G": ["procter", "p g"],
    "Fidelity International": ["fidelity international", "fil"],
}


def jds_dir() -> Path:
    return ledger.PRIVATE / "jds"


def reports_dir() -> Path:
    return ledger.PRIVATE / "reports"


def _safe(k: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", k).strip("-")


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- JD copies

def write_jd(k: str, e: dict, text: str, source: str, at: str | None = None) -> Path:
    at = at or state.now_iso()
    path = jds_dir() / f"{_safe(k)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [f"# {e.get('board', '')}: {e.get('title', k)}", "",
            f"- key: {k}", f"- requisition: {e.get('req') or 'not stated'}", f"- JD page: {e.get('url', '')}",
            f"- apply: {e.get('apply_url', '')}",
            f"- posted: {e.get('posted_at') or 'not stated'}", f"- saved: {at} (source: {source})", ""]
    path.write_text("\n".join(head) + "\n" + text.strip() + "\n", encoding="utf-8")
    e.update(jd_file=path.name, jd_saved_at=at, jd_source=source)
    e.pop("jd_error", None)
    e.pop("jd_tries", None)
    return path


def save_alert_jd(e: dict, k: str, text: str, source: str) -> None:
    """At alert time (run.py): the JD the screener read, or the one fetched for the alert."""
    if text.strip():
        write_jd(k, e, text, source)


def job_from_entry(e: dict) -> Job:
    return Job(board=e.get("board", ""), id=str(e.get("id", "")), title=e.get("title", ""), locations=[],
               url=e.get("url", ""), apply_url=e.get("apply_url", ""), posted_at=e.get("posted_at"),
               req_id=e.get("req"), check_urls=[e["check_url"]] if e.get("check_url") else [])


def board_kinds() -> dict[str, str]:
    from detector import run
    try:
        return {b["name"]: run.kind(b) for b in run.load_boards(None)}
    except Exception:
        return {}


def fetch_jd(c: httpx.Client, e: dict, kind: str) -> tuple[str, str]:
    from detector import jd, sources
    job = job_from_entry(e)
    hooks = sources.detail_hooks(kind)
    if hooks:
        try:
            hooks[0](c, job)
        except Exception as err:
            return "", f"unknown (detail check: {str(err)[:100]})"
    return jd.get(c, job)


def capture_jds(c: httpx.Client, d: dict, kinds: dict[str, str], limit: int = JD_PER_PASS) -> int:
    """Save missing JD copies for APPLIED, then SHOWN roles not known to be closed. Returns how many saved."""
    todo = [(k, e) for k, e in d["roles"].items()
            if e.get("status") in (ledger.APPLIED, ledger.SHOWN) and not e.get("jd_file")
            and e.get("jd_tries", 0) < JD_TRIES and not e.get("closed_at") and e.get("url")]
    todo.sort(key=lambda r: (r[1]["status"] != ledger.APPLIED, r[1].get("shown_at") or ""))
    saved = 0
    for k, e in todo[:limit]:
        text, src = fetch_jd(c, e, kinds.get(e.get("board", ""), ""))
        if text.strip() and not src.startswith("unknown"):
            write_jd(k, e, text, src)
            saved += 1
            print(f"outcomes: JD saved for {k} ({src})")
        else:
            e["jd_tries"] = e.get("jd_tries", 0) + 1
            e["jd_error"] = src
            print(f"outcomes: JD not saved for {k}: {src} (try {e['jd_tries']} of {JD_TRIES})")
    return saved


# ---------------------------------------------------------------- closures

def role_open(c: httpx.Client, e: dict, kind: str) -> tuple[bool | None, str]:
    """True open, False closed, None unconfirmed. Uses the board's detail hook where it has one."""
    from detector import sources
    hooks = sources.detail_hooks(kind)
    if not hooks:
        from detector.remind import still_open
        return still_open(c, e)
    job = job_from_entry(e)
    try:
        hooks[0](c, job)
    except Exception as err:
        why = str(err)[:120]
        return (False if CLOSED_RE.search(why) else None), f"detail: {why}"
    ok, why = hooks[1](c, job)
    if ok:
        return True, why
    return (False if CLOSED_RE.search(why) else None), why


def check_closed(c: httpx.Client, d: dict, kinds: dict[str, str], now: datetime | None = None, force: bool = False) -> int:
    """About once a day per APPLIED role until it closes. Returns how many were found closed."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    closed = 0
    for k, e in d["roles"].items():
        if e.get("status") != ledger.APPLIED or e.get("closed_at"):
            continue
        last = _ts(e.get("open_checked_at"))
        if not force and last and now - last < CHECK_EVERY:
            continue
        ok, why = role_open(c, e, kinds.get(e.get("board", ""), ""))
        e["open_checked_at"] = stamp
        if ok:
            e["last_open_at"] = stamp
            e.pop("open_check_error", None)
        elif ok is False:
            e["closed_at"], e["closed_why"] = stamp, why
            e.pop("open_check_error", None)
            closed += 1
            print(f"outcomes: {k} closed ({why})")
        else:
            e["open_check_error"] = why
    return closed


def run(c: httpx.Client | None = None, now: datetime | None = None) -> None:
    """Called by sync.py after each pass: catch up JD copies, then the daily closure checks."""
    if not ledger.enabled() or not ledger.LEDGER_FILE.exists():
        return
    d = ledger.load()
    kinds = board_kinds()
    own = c is None
    if own:
        from detector import sources
        c = sources.client()
    try:
        capture_jds(c, d, kinds)
        check_closed(c, d, kinds, now)
    finally:
        if own:
            c.close()
    ledger.save(d)


# ---------------------------------------------------------------- events

def stage_of(text: str) -> str | None:
    t = (text or "").strip().lower().replace("-", " ")
    if t.replace(" ", "_") in STAGES:
        return t.replace(" ", "_")
    for stage, pat in SYNONYMS:
        if re.search(pat, t):
            return stage
    return None


def add_event(e: dict, stage: str, at: str, source: str, note: str = "") -> bool:
    """Adds a dated event; the same stage on the same date is recorded once. Returns True if new."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    evs = e.setdefault("events", [])
    if any(x["stage"] == stage and x["at"][:10] == at[:10] for x in evs):
        return False
    ev = {"stage": stage, "at": at, "source": source}
    if note:
        ev["note"] = note[:200]
    evs.append(ev)
    evs.sort(key=lambda x: x["at"])
    return True


def norm_company(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    s = COMPANY_NOISE.sub(" ", s)
    return " ".join(s.split())


def norm_title(s: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", (s or "").lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def company_matches(board: str, company: str) -> bool:
    c = norm_company(company)
    if not c:
        return False
    b = norm_company(board)
    names = [b] + [norm_company(a) for a in ALIASES.get(board, [])]
    padded = f" {c} "
    return any(n and (f" {n} " in padded or f" {c} " in f" {n} ") for n in names)


def match(d: dict, row: dict) -> tuple[str | None, str, list[str]]:
    """(key, how, candidates). Only APPLIED roles are candidates."""
    applied = {k: e for k, e in d["roles"].items() if e.get("status") == ledger.APPLIED}
    req = (row.get("req") or "").strip().lower()
    if req:
        hits = [k for k, e in applied.items() if req in (str(e.get("req", "")).lower(), str(e.get("id", "")).lower())]
        if len(hits) == 1:
            return hits[0], "requisition id", hits
    same_co = [k for k, e in applied.items() if company_matches(e.get("board", ""), row.get("company", ""))]
    if not same_co:
        return None, "no applied role at this company", []
    t = norm_title(row.get("title", ""))
    if t:
        scored = sorted(((SequenceMatcher(None, t, norm_title(applied[k].get("title", ""))).ratio(), k) for k in same_co),
                        reverse=True)
        exact = [k for r, k in scored if r == 1.0]
        if len(exact) == 1:
            return exact[0], "company and title", same_co
        close = [k for r, k in scored if r >= 0.85]
        if len(close) == 1:
            return close[0], f"company and title (similarity {scored[0][0]:.2f})", same_co
        if len(same_co) == 1 and scored[0][0] >= 0.6:
            return same_co[0], f"only applied role at this company (title similarity {scored[0][0]:.2f})", same_co
        return None, "title matches no single applied role here", same_co
    if len(same_co) == 1:
        return same_co[0], "only applied role at this company (no title in the email)", same_co
    return None, "several applied roles here and no title", same_co


HEADERS = {"date": "date", "when": "date", "company": "company", "employer": "company", "role": "title",
           "role title": "title", "title": "title", "job title": "title", "position": "title",
           "req": "req", "req id": "req", "requisition": "req", "requisition id": "req", "job id": "req",
           "stage": "stage", "status": "stage", "outcome": "stage", "evidence": "note", "subject": "note",
           "note": "note", "notes": "note", "email subject": "note"}


def parse_report(text: str) -> list[dict]:
    """Rows from a CSV or a markdown table, with headers mapped to date/company/title/req/stage/note."""
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("```")]
    table = [l for l in lines if l.strip().startswith("|")]
    if table:
        rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in table
                if not re.fullmatch(r"\|?[\s:|-]+\|?", l.strip())]
    else:
        start = next((i for i, l in enumerate(lines) if "," in l and re.search(r"stage|status|outcome", l, re.I)), 0)
        rows = list(csv.reader(io.StringIO("\n".join(lines[start:]))))
    if not rows:
        return []
    head = [HEADERS.get(re.sub(r"[_\s]+", " ", h.strip().lower()), "") for h in rows[0]]
    out = []
    for r in rows[1:]:
        row = {h: v.strip() for h, v in zip(head, r) if h}
        if any(row.values()):
            out.append(row)
    return out


def parse_date(s: str) -> str | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


def import_rows(d: dict, rows: list[dict]) -> dict:
    res = {"added": [], "known": [], "unmatched": [], "bad": []}
    for row in rows:
        stage, at = stage_of(row.get("stage", "")), parse_date(row.get("date", ""))
        label = f"{row.get('date', '?')} · {row.get('company', '?')} · {row.get('title', '?')} · {row.get('stage', '?')}"
        if not stage or not at:
            res["bad"].append((label, "unreadable stage" if not stage else "unreadable date"))
            continue
        k, how, cands = match(d, row)
        if not k:
            res["unmatched"].append((label, how, cands))
            continue
        new = add_event(d["roles"][k], stage, at, "inbox report", row.get("note", ""))
        res["added" if new else "known"].append((label, k, how, stage))
    return res


# ---------------------------------------------------------------- report

def summary(e: dict, now: datetime) -> dict:
    evs = e.get("events") or []
    furthest = max((PROGRESS.get(x["stage"], 0) for x in evs), default=0)
    ended = next((x["stage"] for x in reversed(evs) if x["stage"] in ("rejected", "withdrawn", "offer")), None)
    replies = [x for x in evs if x["stage"] != "acknowledged"]
    applied = _ts(e.get("marked_at"))
    first = _ts(replies[0]["at"]) if replies else None
    if ended:
        outcome = ended
    elif furthest >= 2:
        outcome = "in process"
    elif applied and now - applied >= NO_REPLY_AFTER:
        outcome = "no reply"
    else:
        outcome = "waiting"
    from detector.notify import IST  # email events carry a date only, so count calendar days (IST)
    days = (first.date() - applied.astimezone(IST).date()).days if first and applied else None
    return {"reply": bool(replies), "progressed": furthest >= 2, "outcome": outcome, "days_to_reply": days}


def age_at_apply(e: dict) -> str:
    applied, posted = _ts(e.get("marked_at")), e.get("posted_at")
    if not applied or not posted:
        return "posting date not stated"
    from detector.notify import IST
    days = (applied.astimezone(IST).date() - datetime.fromisoformat(posted[:10]).date()).days
    return "same day" if days <= 0 else "1-2 days" if days <= 2 else "3-7 days" if days <= 7 else "8+ days"


def cov_band(e: dict) -> str:
    c = e.get("coverage")
    if c is None:
        return "no checklist"
    return "≥0.90" if c >= 0.9 else "0.75-0.89" if c >= 0.75 else "0.55-0.74" if c >= 0.55 else "<0.55"


def table(title: str, groups: dict[str, list[dict]]) -> list[str]:
    out = [f"### {title}", "", "| | applied | any reply | progressed | rejected | no reply | waiting |", "|---|---|---|---|---|---|---|"]
    for name, rows in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0])):
        n = len(rows)
        cnt = lambda f: sum(1 for r in rows if f(r))
        out.append(f"| {name} | {n} | {cnt(lambda r: r['reply'])} | {cnt(lambda r: r['progressed'])} | "
                   f"{cnt(lambda r: r['outcome'] == 'rejected')} | {cnt(lambda r: r['outcome'] == 'no reply')} | "
                   f"{cnt(lambda r: r['outcome'] == 'waiting')} |")
    return out + [""]


def report(d: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    rows = []
    for k, e in d["roles"].items():
        if e.get("status") == ledger.APPLIED:
            s = summary(e, now)
            s.update(key=k, e=e)
            rows.append(s)
    from detector.notify import IST
    lines = [f"# jobwatch outcomes, {now.astimezone(IST):%d %b %Y}", ""]
    if not rows:
        return "\n".join(lines + ["No APPLIED roles yet."]) + "\n"
    n = len(rows)
    with_events = sum(1 for r in rows if r["e"].get("events"))
    days = [r["days_to_reply"] for r in rows if r["days_to_reply"] is not None]
    closed = [r for r in rows if r["e"].get("closed_at")]
    lines += [f"- Applied through jobwatch: {n} (with any recorded email event: {with_events})",
              f"- Any reply beyond an acknowledgement: {sum(r['reply'] for r in rows)}; progressed to an assessment "
              f"or further: {sum(r['progressed'] for r in rows)}",
              f"- Outcome: " + ", ".join(f"{o} {sum(1 for r in rows if r['outcome'] == o)}"
                                       for o in ("offer", "in process", "rejected", "withdrawn", "no reply", "waiting")),
              f"- Days from applying to the first reply: "
              + (f"median {statistics.median(days):g} (n={len(days)})" if days else "no replies recorded"),
              f"- Postings closed since applying: {len(closed)}; JD copies saved: "
              f"{sum(1 for r in rows if r['e'].get('jd_file'))} of {n}",
              f"- \"No reply\" means nothing but an acknowledgement {NO_REPLY_AFTER.days}+ days after applying. "
              f"Small groups: read counts, not percentages.", ""]
    by = lambda f: {name: [r for r in rows if f(r) == name] for name in {f(r) for r in rows}}
    lines += table("By verdict", by(lambda r: r["e"].get("verdict") or "not screened"))
    lines += table("By fit", by(lambda r: f"fit {r['e'].get('fit') if r['e'].get('fit') is not None else '?'}"))
    lines += table("By required coverage", by(lambda r: cov_band(r["e"])))
    lines += table("By posting age when applied", by(lambda r: age_at_apply(r["e"])))
    lines += table("By company", by(lambda r: r["e"].get("board", "?")))
    lines += ["### Roles", "", "| applied | company | role | verdict | outcome | events | closed |", "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["e"].get("marked_at") or "", reverse=True):
        e = r["e"]
        ev = "; ".join(f"{x['stage']} {x['at'][:10]}" for x in e.get("events") or []) or "none"
        v = f"{e.get('verdict', '')} {e.get('fit', '')}".strip() or "not screened"
        lines.append(f"| {(e.get('marked_at') or '')[:10]} | {e.get('board')} | {e.get('title', r['key'])} | {v} | "
                     f"{r['outcome']} | {ev} | {(e.get('closed_at') or '')[:10]} |")
    return "\n".join(lines) + "\n"


PROMPT = """Search my Gmail inbox for emails about the job applications listed below (application
confirmations, online assessments, recruiter messages, interview invitations, offers, rejections),
from {since} onward. Look at the sender's company and any job title or requisition number in the
email. Include only emails about these applications.

Reply with only a CSV, no other text, with this header:
date,company,role_title,req_id,stage,evidence

- date: the email's date, YYYY-MM-DD
- company: the employer
- role_title: the job title the email names, else blank
- req_id: the requisition / job number the email names, else blank
- stage: one of acknowledged, assessment, recruiter_screen, interview, offer, rejected, withdrawn
- evidence: the email subject line
One row per email. If an email covers several roles, one row per role. Quote any field containing a comma.

The applications (applied date, company, title, requisition):
{roles}
"""


def prompt_text(d: dict) -> str:
    roles = sorted((e for e in d["roles"].values() if e.get("status") == ledger.APPLIED),
                   key=lambda e: e.get("marked_at") or "")
    if not roles:
        return "No APPLIED roles in the ledger."
    since = (roles[0].get("marked_at") or "")[:10]
    lines = "\n".join(f"- {(e.get('marked_at') or '')[:10]}, {e.get('board')}, {e.get('title')}, {e.get('req') or 'n/a'}"
                      for e in roles)
    return PROMPT.format(since=since, roles=lines)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="detector.outcomes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prompt")
    p = sub.add_parser("import")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("add")
    p.add_argument("ref")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--at", help="YYYY-MM-DD (default today)")
    p.add_argument("--note", default="")
    p = sub.add_parser("jds")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("closed")
    p.add_argument("--force", action="store_true", help="check every open APPLIED role now")
    sub.add_parser("report")
    args = ap.parse_args(argv)

    from detector.run import load_env
    load_env()
    d = ledger.load()
    if args.cmd == "prompt":
        print(prompt_text(d))
        return 0
    if args.cmd == "report":
        text = report(d)
        print(text)
        reports_dir().mkdir(parents=True, exist_ok=True)
        path = reports_dir() / f"outcomes-{datetime.now():%Y-%m-%d}.md"
        path.write_text(text, encoding="utf-8")
        print(f"(saved to {path})")
        return 0
    if args.cmd == "add":
        hits = [k for k in ledger.find(d, args.ref) if d["roles"].get(k, {}).get("status") == ledger.APPLIED]
        if len(hits) != 1:
            print(f"{args.ref!r}: {'no APPLIED role matches' if not hits else 'ambiguous: ' + ', '.join(hits)}")
            return 1
        at = parse_date(args.at) if args.at else datetime.now().strftime("%Y-%m-%d")
        if not at:
            print(f"unreadable date {args.at!r}")
            return 1
        new = add_event(d["roles"][hits[0]], args.stage, at, "cli", args.note)
        ledger.save(d)
        print(f"{hits[0]}: {args.stage} {at}" + ("" if new else " (already recorded)"))
        return 0
    if args.cmd == "import":
        rows = parse_report(Path(args.file).read_text(encoding="utf-8"))
        res = import_rows(d, rows)
        print(f"{len(rows)} row(s) read")
        for label, k, how, stage in res["added"]:
            print(f"  added   {label}  ->  {k} ({how})")
        for label, k, how, stage in res["known"]:
            print(f"  known   {label}  ->  {k}")
        for label, how, cands in res["unmatched"]:
            print(f"  NOT MATCHED  {label}: {how}" + (f" (candidates: {', '.join(cands)})" if cands else ""))
        for label, why in res["bad"]:
            print(f"  SKIPPED  {label}: {why}")
        print(f"added {len(res['added'])}, already known {len(res['known'])}, not matched {len(res['unmatched'])}, "
              f"unreadable {len(res['bad'])}" + (" (dry run, nothing saved)" if args.dry_run else ""))
        if res["unmatched"]:
            print("Unmatched rows: add them with `outcomes add <board:id> <stage> --at <date>`.")
        if not args.dry_run:
            ledger.save(d)
        return 0
    from detector import sources
    kinds = board_kinds()
    with sources.client() as c:
        if args.cmd == "jds":
            n = capture_jds(c, d, kinds, limit=args.limit)
            print(f"{n} JD copy(ies) saved")
        else:
            n = check_closed(c, d, kinds, force=args.force)
            print(f"{n} newly closed")
    ledger.save(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
