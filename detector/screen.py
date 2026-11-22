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
    try:
        v["fit"] = max(1, min(5, int(v.get("fit", 1))))
    except (TypeError, ValueError):
        v["fit"] = 1
    v["gaps"] = [str(g) for g in v.get("gaps") or []]
    v["skills"] = [str(s).strip() for s in v.get("skills") or [] if str(s).strip()][:12]
    for k in ("location", "experience_asked", "one_line", "out_reason", "out_short", "resume_tweak"):
        v[k] = str(v.get(k) or "")
    return v


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


def save_kit(job: Job, v: dict) -> Path:
    KITS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{job.board}-{job.id}").strip("-")
    path = KITS / f"{datetime.now(IST):%Y-%m-%d}-{safe}.md"
    gaps = "\n".join(f"- {g}" for g in v["gaps"]) or "- none named"
    path.write_text(f"""# {job.board}: {job.title}

**{v['verdict']}** · fit {v['fit']}/5 · {v['location']}
Experience asked: {v['experience_asked']}

{v['one_line']}

## Gaps
{gaps}

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
