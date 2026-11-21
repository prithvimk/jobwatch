"""On-demand sweep: everything a company has open in India right now, not only what is new.

The detector alerts only on postings it has not seen, so roles already open when a board was first
tracked (baselined) never alert. A sweep enumerates the full India board (every page), reports the
counts, lists the title hits the prefilter dropped and why, pulls the JD of every candidate, and with
--screen judges them with the screener (cached verdicts are reused). It pushes nothing and writes no
state/; the report goes to stdout and private/sweeps/<date>-<company>.md.

  uv run python -m detector.sweep JPMC
  uv run python -m detector.sweep "Bank of America" Visa --screen
  uv run python -m detector.sweep JPMC --screen --screen-limit 10
"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from detector import jd, ledger, notify, screen, state
from detector.prefilter import Prefilter
from detector.sources import FETCHERS, Job, client, detail_hooks

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "private" / "sweeps"
SCREEN_LIMIT = 25  # Claude calls per sweep unless --screen-limit says otherwise (Pro usage)
YEARS = re.compile(r"[^.\n]{0,60}\b\d{1,2}\s*(?:\+|-\s*\d{1,2}|to\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b[^.\n]{0,60}", re.I)


@dataclass
class Candidate:
    job: Job
    why: str
    history: str = ""
    jd_source: str = ""
    years_quote: str = ""
    verdict: dict | None = None
    note: str = ""


@dataclass
class Report:
    board: str
    ats: str
    fetched: int = 0
    india: int = 0
    no_location: int = 0
    outside: int = 0
    other_titles: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    dropped_hits: list[tuple[Job, str]] = field(default_factory=list)
    not_open: list[tuple[Job, str]] = field(default_factory=list)
    error: str = ""


def all_boards() -> list[dict]:
    return yaml.safe_load((ROOT / "boards.yaml").read_text(encoding="utf-8"))["boards"]


def resolve(query: str, boards: list[dict]) -> list[dict]:
    """Exact name (any case), else every board whose name contains the query."""
    q = query.lower()
    exact = [b for b in boards if b["name"].lower() == q]
    return exact or [b for b in boards if q in b["name"].lower()]


def classify(jobs: list[Job], pf: Prefilter, r: Report) -> None:
    r.fetched = len(jobs)
    for j in jobs:
        locs = [l for l in j.locations if l and l.strip()]
        if locs and not any(pf.india.search(l) for l in locs):
            r.outside += 1
            continue
        r.india += 1
        r.no_location += not locs
        ok, why = pf.check(j)
        if ok:
            r.candidates.append(Candidate(j, why))
        elif pf.title_hit(j):
            r.dropped_hits.append((j, why))
        else:
            r.other_titles += 1


def history(job: Job, seen: dict, led: dict) -> str:
    """What jobwatch already knows about this posting, so nothing is called new by mistake."""
    k = f"{job.board}:{job.id}"
    e = led.get("roles", {}).get(k)
    if e and e["status"] in ledger.RESOLVED:
        return f"{e['status']} {notify.ist(e['marked_at'])}" if e.get("marked_at") else e["status"]
    prior = ledger.same_req(led, job)
    if prior and prior["status"] in ledger.RESOLVED:
        return f"repost of a {prior['status']} requisition ({prior.get('board')}:{prior.get('id')})"
    if e:
        return f"alerted {notify.ist(e['shown_at'])}, no Applied/Skip" if e.get("shown_at") else "alerted"
    s = seen.get("boards", {}).get(job.board, {}).get("jobs", {}).get(job.id)
    if not s:
        return "new: not seen by the detector yet"
    first = notify.ist(s["first_seen"])
    return {
        "baseline": f"open since before tracking (first seen {first}), never alerted",
        "alerted": f"alerted {notify.ist(s.get('alerted_at') or s['first_seen'])}",
        "screened": f"screened OUT {first}",
        "filtered": f"filtered by the detector {first}",
        "pending": f"pending liveness since {first}",
        "dead": f"marked dead {first} (failed liveness)",
        "repost": f"repost suppressed {first}",
    }.get(s.get("status"), f"seen {first} ({s.get('status')})")


def years_quote(text: str) -> str:
    """The first phrase in the JD that states a number of years, quoted verbatim (not judged)."""
    m = YEARS.search(text or "")
    if not m:
        return ""
    start, end = m.span()
    t = text or ""
    q = m.group(0)
    if start > 0 and t[start - 1].isalnum():  # began mid-word: drop the fragment
        q = q.split(" ", 1)[1] if " " in q else q
    if end < len(t) and t[end].isalnum():     # ended mid-word: drop the fragment
        q = q.rsplit(" ", 1)[0] if " " in q else q
    return " ".join(q.split()).strip(" ,;:-")


def sweep_board(c: httpx.Client, b: dict, pf: Prefilter) -> tuple[Report, list[Candidate]]:
    from detector.run import kind
    r = Report(b["name"], kind(b))
    if r.ats not in FETCHERS:
        r.error = f"unknown: no fetcher for this board ({b.get('effort') or b.get('ats')}); not swept"
        return r, []
    india_ok = lambda loc: bool(pf.india.search(loc))  # north India too, so it is counted and reported
    try:
        jobs = FETCHERS[r.ats](c, b, skip=set(), location_ok=india_ok, full=True)
    except Exception as e:
        r.error = f"unknown: fetch failed ({type(e).__name__}: {str(e)[:150]})"
        return r, []
    classify(jobs, pf, r)

    hooks = detail_hooks(r.ats)
    kept = []
    for cand in r.candidates:
        if hooks:
            try:
                hooks[0](c, cand.job)
            except Exception as e:
                r.not_open.append((cand.job, f"detail API: {type(e).__name__}: {str(e)[:100]}"))
                continue
            ok, why = pf.check(cand.job)
            if not ok:  # the detail filled in locations (Workday "N Locations" rows)
                r.dropped_hits.append((cand.job, f"{why} (after detail)"))
                continue
            cand.why = why
        kept.append(cand)
    r.candidates = kept
    return r, kept


def run(names: list[str], screen_roles: bool = False, screen_limit: int = SCREEN_LIMIT,
        out_dir: Path | None = None) -> list[Report]:
    pf = Prefilter.load()
    boards = all_boards()
    seen, led, verdicts = state.load_seen(), ledger.load(), screen.load_verdicts()
    reports: list[Report] = []
    with client() as c:
        for q in names:
            matched = resolve(q, boards)
            if not matched:
                reports.append(Report(q, "", error="unknown: no board by that name in boards.yaml"))
                continue
            for b in matched:
                r, cands = sweep_board(c, b, pf)
                for cand in cands:
                    cand.history = history(cand.job, seen, led)
                    text, cand.jd_source = jd.get(c, cand.job)
                    cand.job.jd = text or cand.job.jd
                    cand.years_quote = years_quote(text)
                    cand.verdict = verdicts.get(f"{cand.job.board}:{cand.job.id}")
                reports.append(r)

    if screen_roles:
        ok, why = screen.available()
        todo = [cand for r in reports for cand in r.candidates
                if cand.verdict is None and not cand.history.startswith(ledger.RESOLVED)
                and not cand.history.startswith("repost of a")]
        if not ok:
            for cand in todo:
                cand.note = f"Screening unavailable: {why}."
        else:
            for cand in todo[screen_limit:]:
                cand.note = f"not screened (over --screen-limit {screen_limit})"

            def _one(cand: Candidate):
                try:
                    return cand, screen.screen(cand.job, cand.job.jd or "", cand.jd_source), None
                except screen.ScreenError as e:
                    return cand, None, e

            batch = todo[:screen_limit]
            if batch:
                with ThreadPoolExecutor(max_workers=min(4, len(batch))) as pool:
                    for cand, v, err in pool.map(_one, batch):
                        if v is None:
                            cand.note = f"Screening unavailable: {str(err)[:120]}."
                            continue
                        cand.verdict = v
                        verdicts[f"{cand.job.board}:{cand.job.id}"] = v
                        if v["verdict"] != "OUT":
                            cand.note = f"kit: {screen.save_kit(cand.job, v).name}"
                screen.save_verdicts(verdicts)

    out_dir = out_dir or OUT_DIR
    for r in reports:
        text = render(r)
        print(text)
        if not r.error or r.fetched:
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "-", r.board).strip("-")
            path = out_dir / f"{datetime.now(notify.IST):%Y-%m-%d}-{safe}.md"
            path.write_text(text, encoding="utf-8")
            print(f"(saved to {path})\n")
    return reports


def _rank(cand: Candidate) -> tuple:
    order = {"IN": 3, "STRETCH": 2, "OUT": 0}
    v = cand.verdict or {}
    return (order.get(v.get("verdict"), 1), v.get("fit", 0), cand.job.posted_at or "")


def render(r: Report) -> str:
    when = datetime.now(notify.IST).strftime("%d %b %Y %H:%M IST")
    lines = [f"# Sweep: {r.board} ({r.ats or 'no board'}), {when}", ""]
    if r.error:
        return "\n".join(lines + [r.error, ""])
    lines += [f"- Postings fetched: {r.fetched}" + (f" ({r.outside} outside India)" if r.outside else ""),
              f"- India: {r.india}" + (f" ({r.no_location} with no location stated)" if r.no_location else ""),
              f"- Candidates (title and location pass): {len(r.candidates)}",
              f"- Title hits dropped by the prefilter: {len(r.dropped_hits)}",
              f"- Other India titles (not GenAI/ML): {r.other_titles}"]
    if r.not_open:
        lines.append(f"- Listed but the detail API says closed or failed: {len(r.not_open)}")
    lines += ["", "## Candidates", ""]
    if not r.candidates:
        lines.append("None.")
    for cand in sorted(r.candidates, key=_rank, reverse=True):
        j, v = cand.job, cand.verdict
        posted = f"posted {notify.ist(j.posted_at)} (per ATS)" if j.posted_at else "posting date not stated"
        closes = f", closes {notify.ist(j.closes_at)}" if j.closes_at else ""
        head = f"**{v['verdict']} fit {v['fit']}/5** " if v else ""
        lines.append(f"- {head}{j.title} · {'; '.join(l for l in j.locations if l) or 'location not stated'}")
        lines.append(f"  - {posted}{closes} · {cand.history}")
        if v:
            lines.append(f"  - {v['one_line']} Asks: {v['experience_asked']}.")
            if v["verdict"] == "OUT":
                lines.append(f"  - Out: {v['out_reason']}")
            elif v["gaps"]:
                lines.append(f"  - Gaps: {'; '.join(v['gaps'][:4])}")
        elif cand.years_quote:
            lines.append(f"  - JD says: \"{cand.years_quote}\"")
        if cand.jd_source and not cand.jd_source.startswith(("ATS API", "Greenhouse", "SmartRecruiters")):
            lines.append(f"  - JD source: {cand.jd_source}")
        if cand.note:
            lines.append(f"  - {cand.note}")
        lines.append(f"  - {j.url}")
    if r.dropped_hits:
        lines += ["", "## Title hits dropped by the prefilter", ""]
        for j, why in sorted(r.dropped_hits, key=lambda x: x[1]):
            lines.append(f"- {j.title} · {'; '.join(l for l in j.locations if l) or 'location not stated'} · {why} · {j.url}")
    if r.not_open:
        lines += ["", "## Listed but not confirmed open", ""]
        lines += [f"- {j.title} · {why} · {j.url}" for j, why in r.not_open]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="detector.sweep")
    ap.add_argument("company", nargs="+", help="board name as in boards.yaml (any case; a substring matches)")
    ap.add_argument("--screen", action="store_true", help="judge candidates with the screener (uses Pro usage)")
    ap.add_argument("--screen-limit", type=int, default=SCREEN_LIMIT)
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from detector.run import load_env
    load_env()
    reports = run(args.company, args.screen, args.screen_limit)
    return 1 if any(r.error for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
