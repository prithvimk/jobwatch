"""Status page, laptop only: private/status.html, rewritten at the end of every detector.sync pass.

A static page (no server) to open in a browser instead of running the ledger and log commands:
  - roles to act on: SHOWN ledger entries, best fit first, with the screener's verdict, gaps,
    stated closing date, kit and links. Applied / Skip / Undo buttons POST the same message as the
    phone buttons to the ntfy reply topic, so the next pass records them (as with a phone tap).
  - recently resolved roles (APPLIED / SKIPPED, last 30 days)
  - runner: when this page was written (the page warns once that is over 15 min ago), the last
    state commit and which runner made it, and problems logged by the current pass
  - boards: health, IDs tracked, new postings in the last 7 days, pending, alerted, last new posting
  - the OUT roles queued for tonight's digest

The page carries the reply topic (and NTFY_TOKEN if set), so it stays in private/ like the ledger.

  uv run python -m detector.status    # write it now and print its path
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import yaml

from detector import ledger, remind, screen, state
from detector.sources import FETCHERS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "private" / "status.html"
LOG = ROOT / "private" / "logs" / "sync.log"
IST = remind.IST
STALE_MIN = 15
RECENT = timedelta(days=7)
RESOLVED_WINDOW = timedelta(days=30)
PROBLEM = re.compile(r"failed|UNKNOWN|Traceback|Error|unavailable", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _t(s: str | None) -> datetime | None:
    return remind.when(s)


def _ist(s: str | None, date_only: bool = False) -> str:
    t = _t(s)
    if not t:
        return ""
    if date_only or (s and len(s) == 10):
        return t.astimezone(IST).strftime("%d %b")
    return t.astimezone(IST).strftime("%d %b %H:%M")


def _ago(t: datetime | None, now: datetime) -> str:
    if not t:
        return ""
    m = int((now - t).total_seconds() // 60)
    if m < 60:
        return f"{m} min ago"
    if m < 48 * 60:
        return f"{m // 60} h ago"
    return f"{m // 1440} d ago"


def _link(url: str | None, text: str) -> str:
    if not url or not url.startswith(("http://", "https://", "file:")):
        return escape(text)
    return f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(text)}</a>'


def _kit(board: str, job_id: str) -> Path | None:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{board}-{job_id}").strip("-")
    hits = sorted(screen.KITS.glob(f"*-{safe}.md")) if screen.KITS.is_dir() else []
    return hits[-1] if hits else None


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- sections

def roles_section(led: dict, verdicts: dict, now: datetime) -> str:
    shown = [(k, e) for k, e in led["roles"].items() if e.get("status") == ledger.SHOWN]
    shown.sort(key=lambda r: (r[1].get("fit") or 0, r[1].get("shown_at") or ""), reverse=True)
    if not shown:
        return '<p class="muted">Nothing waiting on you.</p>'
    rows = []
    for k, e in shown:
        v = verdicts.get(k, {})
        closes = _t(e.get("closes_at"))
        cls = ""
        close_txt = "not stated"
        if closes:
            left = closes - now
            close_txt = f"{_ist(e['closes_at'])}" + (" (closed?)" if left.total_seconds() < 0 else "")
            cls = "urgent" if left <= timedelta(days=2) else ""
        if e.get("closed_at"):
            close_txt, cls = f"closed early, seen {_ist(e['closed_at'])}", "urgent"
        verdict = e.get("verdict") or v.get("verdict")
        fit = e.get("fit") or v.get("fit")
        badge = (f'<span class="badge {escape(verdict.lower())}">{escape(verdict)}</span> {fit}/5'
                 if verdict else '<span class="muted">not screened</span>')
        gaps = v.get("gaps") or []
        detail = escape(v.get("one_line", ""))
        if gaps:
            detail += "<br><span class='muted'>Gaps:</span> " + escape("; ".join(gaps))
        if v.get("location"):
            detail += "<br><span class='muted'>Location:</span> " + escape(v["location"])
        if v.get("experience_asked"):
            detail += "<br><span class='muted'>Experience:</span> " + escape(v["experience_asked"])
        kit = _kit(e.get("board", ""), e.get("id", ""))
        links = " · ".join(x for x in (
            _link(e.get("apply_url"), "Apply"), _link(e.get("url"), "JD"),
            _link(kit.as_uri(), "Kit") if kit else "") if x)
        posted = f"posted {_ist(e['posted_at'])}" if e.get("posted_at") else ""
        rows.append(f"""<tr data-key="{escape(k)}">
<td><b>{escape(e.get('board', ''))}</b><br>{escape(e.get('title', '(title unknown: alerted from Actions)'))}
<div class="muted small">{escape(e.get('req') or e.get('id', ''))} · shown {_ist(e.get('shown_at'))} {posted}</div></td>
<td class="nowrap">{badge}</td>
<td>{detail}</td>
<td class="nowrap {cls}">{close_txt}</td>
<td class="nowrap">{links}</td>
<td class="nowrap act"><button data-s="applied">Applied</button> <button data-s="skipped">Skip</button>
<span class="pending muted"></span></td></tr>""")
    return ("<table><tr><th>Role</th><th>Verdict</th><th>Fit notes</th><th>Closes</th><th>Links</th><th>Mark</th></tr>"
            + "".join(rows) + "</table>")


def resolved_section(led: dict, now: datetime) -> str:
    rows = [(k, e) for k, e in led["roles"].items()
            if e.get("status") in ledger.RESOLVED and (t := _t(e.get("marked_at"))) and now - t <= RESOLVED_WINDOW]
    if not rows:
        return '<p class="muted">None in the last 30 days.</p>'
    rows.sort(key=lambda r: r[1].get("marked_at", ""), reverse=True)
    out = []
    for k, e in rows:
        follow = f"follow-up sent {_ist(e['followed_up_at'])}" if e.get("followed_up_at") else ""
        out.append(f"""<tr data-key="{escape(k)}"><td><b>{escape(e.get('board', ''))}</b> {escape(e.get('title', ''))}</td>
<td><span class="badge {e['status'].lower()}">{e['status']}</span></td><td class="nowrap">{_ist(e.get('marked_at'))}</td>
<td class="muted">{follow}</td><td class="nowrap">{_link(e.get('url'), 'JD')}</td>
<td class="act"><button data-s="shown">Undo</button> <span class="pending muted"></span></td></tr>""")
    return "<table><tr><th>Role</th><th>Mark</th><th>When</th><th></th><th></th><th></th></tr>" + "".join(out) + "</table>"


def _last_state_commit() -> tuple[datetime | None, str]:
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%cI%x09%s"], cwd=state.STATE_DIR, capture_output=True,
                           text=True, encoding="utf-8", timeout=10, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    if r.returncode or "\t" not in r.stdout:
        return None, ""
    when, subject = r.stdout.strip().split("\t", 1)
    runner = re.search(r"\((\w+)\)\s*$", subject)
    return datetime.fromisoformat(when), runner.group(1) if runner else subject


def _pass_log() -> list[str]:
    """Problem lines logged since the last pass header (the pass writing this page, when run by sync)."""
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-600:]
    start = max((i for i, ln in enumerate(lines) if ln.startswith("--- ")), default=0)
    return [ln.rstrip() for ln in lines[start:] if PROBLEM.search(ln)][:30]


def runner_section(now: datetime) -> str:
    commit_at, runner = _last_state_commit()
    probs = _pass_log()
    ok_screen, why = screen.available()
    items = [
        f"<li>Page written {now.astimezone(IST):%d %b %H:%M} IST <span id='age' class='muted'></span></li>",
        "<li>Last state commit: " + (f"{commit_at.astimezone(IST):%d %b %H:%M} IST by {escape(runner)} "
                                     f"<span class='muted'>({_ago(commit_at, now)}; healthy passes commit nothing)</span>"
                                     if commit_at else "<span class='muted'>unknown</span>") + "</li>",
        f"<li>Screening: {'on' if ok_screen else 'off (' + escape(why) + ')'}</li>",
    ]
    log = ("<details open><summary>Problems logged this pass</summary><pre>" + escape("\n".join(probs)) + "</pre></details>"
           if probs else '<p class="muted">No problems logged this pass.</p>')
    return "<ul>" + "".join(items) + "</ul>" + log


def boards_section(seen: dict, health: dict, now: datetime) -> str:
    cfg = yaml.safe_load((ROOT / "boards.yaml").read_text(encoding="utf-8"))["boards"]
    rows = []
    for b in cfg:
        name = b["name"]
        k = b.get("platform", "") if b.get("ats") == "custom" else b.get("ats", "")
        if k not in FETCHERS:
            rows.append((2, name, f"<tr class='dim'><td>{escape(name)}</td><td>{escape(k or 'unknown')}</td>"
                                  "<td><span class='badge unknown'>unknown</span> not watched</td><td colspan=6></td></tr>"))
            continue
        bs = seen.get("boards", {}).get(name, {})
        jobs = bs.get("jobs", {})
        h = health.get(name, {})
        if not h.get("ok", True):
            st = (f"<span class='badge out'>failing</span> {h.get('fails', 0)} passes since {_ist(h.get('failing_since'))}"
                  f"<div class='small muted'>{escape(h.get('error', ''))}</div>")
            rank = 0
        elif not bs:
            st, rank = "<span class='badge unknown'>no state yet</span>", 1
        else:
            st, rank = "<span class='badge ok'>OK</span>", 3
        later = [e for e in jobs.values() if e.get("status") != "baseline"]
        recent = [e for e in later if (t := _t(e.get("first_seen"))) and now - t <= RECENT]
        c = Counter(e.get("status") for e in jobs.values())
        last_new = max((e["first_seen"] for e in later if e.get("first_seen")), default="")
        rows.append((rank, name, f"<tr><td>{escape(name)}</td><td>{escape(k)}</td><td>{st}</td>"
                                 f"<td class='nowrap'>{_ist(bs.get('baseline_at'), True)}</td><td class='num'>{len(jobs)}</td>"
                                 f"<td class='num'>{len(recent) or ''}</td><td class='num'>{c['pending'] or ''}</td>"
                                 f"<td class='num'>{c['alerted'] or ''}</td><td class='nowrap'>{_ist(last_new) or '—'}</td></tr>"))
    rows.sort(key=lambda r: (r[0], r[1].lower()))
    return ("<table><tr><th>Board</th><th>Source</th><th>Health</th><th>Tracked since</th><th>IDs</th>"
            "<th>New 7d</th><th>Pending</th><th>Alerted</th><th>Last new</th></tr>" + "".join(r[2] for r in rows) + "</table>")


def activity_section(seen: dict, now: datetime) -> str:
    c: Counter = Counter()
    for bs in seen.get("boards", {}).values():
        for e in bs.get("jobs", {}).values():
            if e.get("status") != "baseline" and (t := _t(e.get("first_seen"))) and now - t <= RECENT:
                c[e.get("status")] += 1
    if not c:
        return '<p class="muted">No new postings in the last 7 days.</p>'
    order = ("alerted", "screened", "filtered", "pending", "dead", "repost")
    return "<p>" + " · ".join(f"<b>{c[s]}</b> {s}" for s in order if c[s]) + "</p>"


def digest_section() -> str:
    d = _load_json(screen.DIGEST, {"pending": [], "last_sent": ""})
    if not d.get("pending"):
        return f'<p class="muted">Nothing queued. Last digest sent: {escape(d.get("last_sent") or "never")}.</p>'
    rows = "".join(f"<tr><td><b>{escape(x['board'])}</b> {_link(x.get('url'), x['title'])}</td>"
                   f"<td>{escape(x['reason'])}</td></tr>" for x in d["pending"])
    return f"<p class='muted'>Sent as one push after 20:00 IST.</p><table><tr><th>Role</th><th>Why OUT</th></tr>{rows}</table>"


# ---------------------------------------------------------------- page

CSS = """
:root{color-scheme:light;--fg:#1a1a1a;--muted:#6b6b6b;--bg:#fff;--line:#e3e3e3;--head:#f5f6f8;--accent:#1f3a5f}
body{font:14px/1.45 system-ui,Segoe UI,sans-serif;color:var(--fg);background:var(--bg);margin:24px auto;max-width:1300px;padding:0 16px}
h1{font-size:20px;margin:0 0 4px;color:var(--accent)} h2{font-size:16px;margin:28px 0 8px;color:var(--accent);border-bottom:1px solid var(--line);padding-bottom:4px}
table{border-collapse:collapse;width:100%} th,td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line)}
th{background:var(--head);font-weight:600;font-size:13px} .num{text-align:right} .nowrap{white-space:nowrap}
.muted{color:var(--muted)} .small{font-size:12px} .dim td{color:var(--muted)} .urgent{color:#c0392b;font-weight:600}
.badge{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12px;font-weight:600;background:#e8e8e8;color:#333}
.badge.in,.badge.ok,.badge.applied{background:#d8f0dd;color:#1e6b2f} .badge.stretch{background:#fdf0cf;color:#8a5a00}
.badge.out{background:#f8d7d3;color:#9b2c1f} .badge.unknown,.badge.skipped{background:#e4e4e4;color:#555}
button{font:inherit;font-size:12px;padding:2px 8px;cursor:pointer} tr.marked{opacity:.55}
#stale{display:none;background:#f8d7d3;color:#9b2c1f;padding:8px 12px;border-radius:6px;margin:8px 0}
@media (max-width:900px){table{display:block;overflow-x:auto} td{min-width:90px}}
pre{white-space:pre-wrap;font-size:12px;background:var(--head);padding:8px;border-radius:6px}
"""

JS = """
const KEY = 'jobwatch-marks';
const marks = JSON.parse(localStorage.getItem(KEY) || '{}');
function show(tr, s, t) {
  tr.classList.add('marked');
  tr.querySelector('.pending').textContent = `${s} sent ${new Date(t).toLocaleTimeString()}: recorded on the next pass`;
  tr.querySelectorAll('button').forEach(b => b.disabled = true);
}
document.querySelectorAll('tr[data-key]').forEach(tr => {
  const m = marks[tr.dataset.key];
  if (m && m.t > GENERATED) show(tr, m.s, m.t);  // clicked after this page was written
  tr.querySelectorAll('button').forEach(b => b.onclick = async () => {
    const s = b.dataset.s, k = tr.dataset.key;
    if (!REPLY) { prompt('No reply topic set. Run this instead:', `uv run python -m detector.ledger ${s} ${k}`); return; }
    try {
      const r = await fetch(REPLY, {method: 'POST', body: `${s} ${k}`, headers: AUTH ? {Authorization: AUTH} : {}});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      marks[k] = {s, t: Date.now()}; localStorage.setItem(KEY, JSON.stringify(marks)); show(tr, s, Date.now());
    } catch (e) {
      prompt(`Sending failed (${e.message}). Run this instead:`, `uv run python -m detector.ledger ${s} ${k}`);
    }
  });
});
function age() {
  const m = Math.floor((Date.now() - GENERATED) / 60000);
  document.getElementById('age').textContent = `(${m} min ago)`;
  const st = document.getElementById('stale');
  st.style.display = m > STALE ? 'block' : 'none';
  st.textContent = `No laptop pass for ${m} min. Check the Task Scheduler job and private/logs/sync.log.`;
}
age(); setInterval(age, 30000);
setTimeout(() => location.reload(), 120000);
"""


def build(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    led = ledger.load()
    verdicts = screen.load_verdicts()
    seen, health = state.load_seen(), state.load_health()
    topic = ledger.reply_topic()
    reply = f"{ledger._server()}/{topic}" if topic else ""
    auth = f"Bearer {os.environ['NTFY_TOKEN']}" if os.environ.get("NTFY_TOKEN") else ""
    n_shown = sum(e.get("status") == ledger.SHOWN for e in led["roles"].values())
    consts = (f"const GENERATED={int(now.timestamp() * 1000)}, STALE={STALE_MIN}, "
              f"REPLY={json.dumps(reply)}, AUTH={json.dumps(auth)};")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>jobwatch ({n_shown} to act on)</title><style>{CSS}</style></head><body>
<h1>jobwatch</h1>
<div id="stale"></div>
<h2>To act on ({n_shown})</h2>{roles_section(led, verdicts, now)}
<h2>Runner</h2>{runner_section(now)}
<h2>Last 7 days</h2>{activity_section(seen, now)}
<h2>Screened out, queued for tonight's digest</h2>{digest_section()}
<h2>Resolved, last 30 days</h2>{resolved_section(led, now)}
<h2>Boards</h2>{boards_section(seen, health, now)}
<script>{consts}{JS}</script>
</body></html>
"""


def write(now: datetime | None = None) -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_name(".status.html.tmp")
    tmp.write_text(build(now), encoding="utf-8")
    tmp.replace(OUT)
    return OUT


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from detector.run import load_env
    load_env()
    print(write().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
