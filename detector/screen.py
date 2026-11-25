"""Screener (M3): one Claude Code CLI call per new prefilter hit, on the laptop only (Pro login, no API key).

Inputs live in private/ (gitignored): resume.md, answers.md, and the filter spec section of
CLAUDE.local.md. The prompt template is prompts/screen.md. Output: a verdict dict (see the template),
a kit file in private/kits/ for IN/STRETCH, and OUT roles queued for the daily digest
(private/digest.json). Any failure raises ScreenError; the caller then sends the plain alert with the
reason, so a screening outage is visible and never drops a role.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from detector.sources import Job

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "private"
KITS = PRIVATE / "kits"
DIGEST = PRIVATE / "digest.json"
VERDICTS_FILE = PRIVATE / "verdicts.json"  # board:id -> verdict, so a retried posting is never re-screened
IST = timezone(timedelta(hours=5, minutes=30))
TIMEOUT_S = 240
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VERDICTS = ("IN", "STRETCH", "OUT")
# Fit is computed here from the requirements checklist Claude fills in, never asked of Claude directly, so the
# same marks always give the same score. Coverage = (shown + half of partial) / required items.
STATUS_CREDIT = {"shown": 1.0, "partial": 0.5, "missing": 0.0}
FIT_BANDS = ((0.9, 5), (0.75, 4), (0.55, 3), (0.35, 2))  # coverage at or above -> fit; below the last band: 1
# An unmet hard bar (degree-only, publications, skill-specific duration) caps fit: missing -> 2, partly shown -> 3.
HARD_BAR_CAP = {"missing": 2, "partial": 3}
# Haiku sometimes renames the fields; these are read as the same thing rather than dropping the item.
REQ_KEYS = ("req", "requirement", "text", "item", "name", "qualification")
STATUS_ALIASES = {"met": "shown", "yes": "shown", "partially shown": "partial", "partly shown": "partial",
                  "partially": "partial", "not shown": "missing", "no": "missing", "unmet": "missing"}
MAX_ITEMS = 15


class ScreenError(RuntimeError):
    pass


def claude_path() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if p.exists():
            return str(p)
    return None


def available() -> tuple[bool, str]:
    if not claude_path():
        return False, "Claude Code CLI not installed"
    missing = [n for n in ("resume.md", "answers.md") if not (PRIVATE / n).exists()]
    if missing:
        return False, f"missing private/{', private/'.join(missing)}"
    if not filter_spec():
        return False, "no '## Filter spec' section in CLAUDE.local.md"
    return True, "ok"


def filter_spec() -> str:
    p = ROOT / "CLAUDE.local.md"
    if not p.exists():
        return ""
    m = re.search(r"^## Filter spec.*?\n(.*?)(?=^## |\Z)", p.read_text(encoding="utf-8"), re.S | re.M)
    return m.group(1).strip() if m else ""


def build_prompt(job: Job, jd: str, jd_source: str) -> str:
    template = (ROOT / "prompts" / "screen.md").read_text(encoding="utf-8")
    return template.format(
        filter_spec=filter_spec(),
        resume=(PRIVATE / "resume.md").read_text(encoding="utf-8"),
        facts=(PRIVATE / "answers.md").read_text(encoding="utf-8"),
        company=job.board, title=job.title, locations="; ".join(job.locations) or "not stated",
        jd_source=jd_source, jd=jd or "(no description text could be fetched)",
    )


def parse(result: str) -> dict:
    """The first JSON object in Claude's reply, validated."""
    start = result.find("{")
    if start < 0:
        raise ScreenError(f"no JSON in reply: {result[:120]!r}")
    try:
        v, _ = json.JSONDecoder().raw_decode(result[start:])
    except ValueError as e:
        raise ScreenError(f"bad JSON in reply: {e}") from None
    v["verdict"] = str(v.get("verdict", "")).upper()
    if v["verdict"] not in VERDICTS:
        raise ScreenError(f"unknown verdict {v.get('verdict')!r}")
    raw_n = len(v["required"]) if isinstance(v.get("required"), list) else 0
    v["required"], v["preferred"] = items(v.get("required")), items(v.get("preferred"))
    if raw_n > len(v["required"]):
        v["checklist_note"] = f"{raw_n - len(v['required'])} of {raw_n} required items unreadable"
    score(v)
    v["skills"] = [str(s).strip() for s in v.get("skills") or [] if str(s).strip()][:12]
    for k in ("location", "experience_asked", "one_line", "out_reason", "out_short", "resume_tweak"):
        v[k] = str(v.get(k) or "")
    return v


def items(raw) -> list[dict]:
    """Checklist items, validated. An item with no text or an unknown status is dropped (it cannot be scored);
    the caller records how many were dropped so an unreadable checklist is visible, never a silent '?'."""
    out = []
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            continue
        req = next((str(x[k]).strip() for k in REQ_KEYS if x.get(k)), "")
        status = str(x.get("status") or "").strip().lower()
        status = STATUS_ALIASES.get(status, status)
        if req and status in STATUS_CREDIT:
            out.append({"req": req, "status": status, "hard": str(x.get("hard")).lower() == "true",
                        "evidence": str(x.get("evidence") or "").strip()})
    return out[:MAX_ITEMS]


def coverage(xs: list[dict]) -> float | None:
    return round(sum(STATUS_CREDIT[x["status"]] for x in xs) / len(xs), 3) if xs else None


def basis(v: dict) -> tuple[list[dict], str]:
    """The items fit is scored on: the required list, or the preferred list when the required section held only
    the overall years and degree (which the checklist leaves out), as in some Visa and Wells Fargo postings."""
    if v.get("required"):
        return v["required"], "required"
    return v.get("preferred") or [], "preferred"


def score(v: dict) -> dict:
    """Set fit (1-5, or None = unknown when no requirement could be read), coverage, pref_coverage and gaps
    from the checklist. OUT is always fit 1. Gaps: unmet hard bars first, then missing, then partial."""
    req, v["fit_basis"] = basis(v)
    v["coverage"], v["pref_coverage"] = coverage(req), coverage(v.get("preferred") or [])
    unmet_hard = [x for x in req if x["hard"] and x["status"] != "shown"]
    if v["verdict"] == "OUT":
        v["fit"] = 1
    elif v["coverage"] is None:
        v["fit"] = None
    else:
        v["fit"] = next((f for low, f in FIT_BANDS if v["coverage"] >= low), 1)
        for x in unmet_hard:
            v["fit"] = min(v["fit"], HARD_BAR_CAP[x["status"]])
    v["gaps"] = ([f"Hard bar: {x['req']}" + (" (partly shown)" if x["status"] == "partial" else "") for x in unmet_hard]
                 + [x["req"] for x in req if x["status"] == "missing" and not x["hard"]]
                 + [f"partial: {x['req']}" for x in req if x["status"] == "partial" and not x["hard"]])
    return v


def fit_text(v: dict | None) -> str:
    """'fit 3/5 (5 of 8 required, 1 partial)'. Verdicts from before the checklist show just the fit."""
    if not v:
        return "not screened"
    f = v.get("fit")
    head = f"fit {f if f is not None else '?'}/5"
    if v.get("verdict") == "OUT" or v.get("required") is None:
        return head
    req, kind = basis(v)
    if not req:
        return head + f" ({v.get('checklist_note') or 'requirements unknown'})"
    shown = sum(x["status"] == "shown" for x in req)
    part = sum(x["status"] == "partial" for x in req)
    return head + f" ({shown} of {len(req)} {kind}" + (f", {part} partial)" if part else ")")


def rank(v: dict | None) -> tuple:
    """Sort key, highest first, used by alerts, the status page, reminders and sweeps: fit, then IN before
    STRETCH at equal fit, then coverage and preferred coverage. Unscreened and unknown fit sort last."""
    if not v:
        return (-1, 0, 0.0, 0.0)
    return (v.get("fit") or 0, 1 if v.get("verdict") == "IN" else 0, v.get("coverage") or 0.0,
            v.get("pref_coverage") or 0.0)


def call_claude(prompt: str) -> str:
    exe = claude_path()
    if not exe:
        raise ScreenError("Claude Code CLI not installed")
    # Run outside the repo so no CLAUDE.md is loaded: the prompt carries everything, and no tools are allowed.
    work = Path(tempfile.gettempdir()) / "jobwatch-screen"
    work.mkdir(exist_ok=True)
    model = os.environ.get("JOBWATCH_SCREEN_MODEL", "haiku")  # Haiku: lightest on Pro usage; matched Sonnet on 30 JPMC verdicts
    args = [exe, "-p", "--output-format", "json", "--max-turns", "1", "--model", model,
            "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task"]
    try:
        p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT_S, cwd=work, creationflags=NO_WINDOW, env={**os.environ, "NO_COLOR": "1"})
    except subprocess.TimeoutExpired:
        raise ScreenError(f"claude timed out after {TIMEOUT_S}s") from None
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise ScreenError(f"claude exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}") from None
    if out.get("is_error") or p.returncode:
        raise ScreenError(f"claude error: {str(out.get('result') or out.get('subtype') or p.stderr)[:200]}")
    return out.get("result") or ""


def screen(job: Job, jd: str, jd_source: str) -> dict:
    v = parse(call_claude(build_prompt(job, jd, jd_source)))
    v["jd_source"] = jd_source
    v["screened_at"] = datetime.now(IST).isoformat(timespec="minutes")
    v["title"] = job.title
    return v


def checklist(v: dict) -> str:
    """The requirements checklist as Markdown tables for the kit (empty for verdicts without one)."""
    mark = {"shown": "✓", "partial": "½", "missing": "✗"}
    out = []
    for head, xs in (("Required", v.get("required") or []), ("Preferred", v.get("preferred") or [])):
        if xs:
            rows = [f"| {mark[x['status']]} | {x['req']}{' (hard bar)' if x['hard'] else ''} | {x['evidence']} |"
                    for x in xs]
            out.append(f"\n## {head}\n| | Requirement | Your evidence |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    return "".join(out)


def save_kit(job: Job, v: dict) -> Path:
    KITS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{job.board}-{job.id}").strip("-")
    path = KITS / f"{datetime.now(IST):%Y-%m-%d}-{safe}.md"
    gaps = "\n".join(f"- {g}" for g in v["gaps"]) or "- none named"
    path.write_text(f"""# {job.board}: {job.title}

**{v['verdict']}** · {fit_text(v)} · {v['location']}
Experience asked: {v['experience_asked']}

{v['one_line']}

## Gaps
{gaps}
{checklist(v)}
## Resume tweak
{v['resume_tweak'] or 'None needed.'}

## Links
- JD: {job.url}
- Apply: {job.apply_url}
- Requisition: {job.req_id or job.id}

## Form facts
{(PRIVATE / 'answers.md').read_text(encoding='utf-8')}
""", encoding="utf-8")
    return path


def queue_out(job: Job, v: dict) -> None:
    d = json.loads(DIGEST.read_text(encoding="utf-8")) if DIGEST.exists() else {"pending": [], "last_sent": ""}
    d["pending"].append({"board": job.board, "title": job.title, "url": job.url,
                         "reason": v["out_reason"] or v["one_line"], "short": v.get("out_short", ""),
                         "at": datetime.now(IST).isoformat(timespec="minutes")})
    DIGEST.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


def digest_due(now: datetime | None = None, hour: int = 20) -> tuple[bool, dict]:
    """Once a day, at the first laptop pass after `hour` IST, if any OUT roles are queued."""
    now = now or datetime.now(IST)
    d = json.loads(DIGEST.read_text(encoding="utf-8")) if DIGEST.exists() else {"pending": [], "last_sent": ""}
    return (bool(d["pending"]) and now.hour >= hour and d.get("last_sent") != now.date().isoformat()), d


DIGEST_MAX_BYTES = 3800  # ntfy turns a message over 4,096 bytes into a file attachment


def clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1].rstrip(" ,;:.-") + "…"


def short_reason(x: dict) -> str:
    """The screener's `out_short`; entries queued before it existed fall back to the clipped full reason."""
    return clip(x.get("short") or x.get("reason", ""), 80)


def digest_text(d: dict) -> str:
    """One block per role (company and title, then the short reason), blank lines between, capped by
    size so ntfy delivers it as a message. Full reasons stay in digest.json and on the status page."""
    rows, out, size = d["pending"], [], 0
    more = lambda n: f"…and {n} more (full reasons on the status page)"
    for i, x in enumerate(rows):
        block = f"{x['board']} · {clip(x['title'], 60)}\n{short_reason(x)}"
        add = len(block.encode()) + (2 if out else 0)
        left = len(rows) - i - 1
        tail = len(more(left).encode()) + 2 if left else 0
        if out and size + add + tail > DIGEST_MAX_BYTES:
            out.append(more(len(rows) - i))
            break
        out.append(block)
        size += add
    return "\n\n".join(out)


def mark_digest_sent(d: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(IST)
    DIGEST.write_text(json.dumps({"pending": [], "last_sent": now.date().isoformat()}, indent=1), encoding="utf-8")


def load_verdicts() -> dict:
    return json.loads(VERDICTS_FILE.read_text(encoding="utf-8")) if VERDICTS_FILE.exists() else {}


def save_verdicts(v: dict) -> None:
    PRIVATE.mkdir(exist_ok=True)
    VERDICTS_FILE.write_text(json.dumps(v, indent=1, ensure_ascii=False), encoding="utf-8")
