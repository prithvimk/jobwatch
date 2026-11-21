"""Detector pass: fetch -> diff against every ID ever seen -> prefilter -> liveness -> push -> save state.

Usage:
  uv run python -m detector.run                 # live: pushes via ntfy (needs NTFY_TOPIC), writes state/
  uv run python -m detector.run --dry-run       # prints pushes, writes nothing
  uv run python -m detector.run --board Sarvam  # limit to one board (repeatable)

A board with no state yet is baselined: its current postings are recorded without alerting, so the
first run does not flood the phone. Use the on-demand sweep (later milestone) for existing postings.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import yaml

from detector import jd, notify, screen, state
from detector.prefilter import Prefilter
from detector.sources import FETCHERS, Job, client, detail_hooks, enrich_smartrecruiters_urls

ROOT = Path(__file__).resolve().parent.parent
CLOSED_URL = ("error=true", "/404", "job-not-found", "jobnotfound")  # closed jobs redirect here
APPLY_ENTRY = ("/oneclick-ui/",)  # ATS application entry paths that refuse non-browser clients
SIGNIN_APPLY_HOSTS = ("account.amazon.jobs",)  # apply links that always go to a sign-in (see live())
FULL_SWEEP_EVERY = 12  # passes; incremental fetchers (Workday) fully re-enumerate one board per pass
CLOSED_TEXT = ("this job is no longer", "position has been filled", "no longer accepting applications",
               "job you are looking for is no longer")


def kind(b: dict) -> str:
    """Fetcher key: the ATS, or for custom-JSON boards the platform (amazon, eightfold, jibe, ...)."""
    return b.get("platform", "") if b.get("ats") == "custom" else b.get("ats", "")


def load_boards(names: list[str] | None) -> list[dict]:
    boards = yaml.safe_load((ROOT / "boards.yaml").read_text(encoding="utf-8"))["boards"]
    boards = [b for b in boards if kind(b) in FETCHERS]
    return [b for b in boards if b["name"] in names] if names else boards


def live(c: httpx.Client, url: str) -> tuple[bool, str]:
    """The page loads (2xx after redirects) and doesn't land on a closed/404 page."""
    host = httpx.URL(url).host if url else ""
    if host in SIGNIN_APPLY_HOSTS:
        # The apply link sends every job, open or not, through a long SSO redirect chain that drops
        # non-browser clients. Only the first hop says anything: it answers (2xx/3xx) at the entry page.
        try:
            r = c.get(url, follow_redirects=False)
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}"
        if r.status_code < 400:
            return True, f"apply link answers ({r.status_code}) and leads to the {host} sign-in"
        return False, f"HTTP {r.status_code}"
    try:
        r = c.get(url)
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}"
    final = str(r.url).lower()
    if r.status_code in (401, 403) and any(p in final for p in APPLY_ENTRY):
        return True, f"redirected to application entry page (bot-protected, HTTP {r.status_code})"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    head = r.text[:20000].lower() if "html" in r.headers.get("content-type", "") else ""
    for m in CLOSED_URL:
        if m in final:
            return False, f"redirected to closed page ({m})"
    for m in CLOSED_TEXT:
        if m in head:
            return False, f"page says {m!r}"
    return True, "ok"


def liveness(c: httpx.Client, job: Job) -> tuple[bool, str]:
    if job.check_urls and "smartrecruiters" in job.check_urls[0]:
        enrich_smartrecruiters_urls(c, job)
    for label, url in [("api", u) for u in job.check_urls] + [("JD", job.url), ("apply", job.apply_url)]:
        ok, why = live(c, url)
        if not ok:
            return False, f"{label} {why}"
    return True, "JD and apply pages load"


def format_alert(job: Job, first_seen: str, why: str, verdict: dict | None = None,
                 note: str = "Screening pending.") -> tuple[str, str]:
    title = f"{job.board}: {job.title}"
    when = (f"Posted {notify.ist(job.posted_at)} (per ATS)" if job.posted_at
            else f"First seen {notify.ist(first_seen)} (posting date not stated)")
    if job.closes_at:
        when += f". Closes {notify.ist(job.closes_at)}"
    if verdict:
        clip = lambda g: g if len(g) <= 100 else g[:97].rstrip() + "..."  # full gaps are in the kit
        gaps = "; ".join(clip(g) for g in verdict["gaps"][:3]) or "none named"
        body = (f"{verdict['verdict']} · fit {verdict['fit']}/5 · {verdict['location']}\n"
                f"{verdict['one_line']}\nGaps: {gaps}\nAsks: {verdict['experience_asked']}\n{when}")
        return f"{verdict['verdict']} {title}", body
    loc = "; ".join(l for l in job.locations if l) or "location unknown"
    body = f"{loc}\n{when}\nPrefilter: {why}. {note}"
    return title, body


def run(names: list[str] | None, dry_run: bool, screen_roles: bool | None = None) -> int:
    """screen_roles: judge each live hit with Claude before alerting (laptop; default from JOBWATCH_SCREEN)."""
    import os
    if screen_roles is None:
        screen_roles = os.environ.get("JOBWATCH_SCREEN") == "1"
    pf = Prefilter.load()
    cfg = yaml.safe_load((Path(__file__).with_name("config.yaml")).read_text(encoding="utf-8"))
    seen, health = state.load_seen(), state.load_health()
    ts = state.now_iso()
    alerts: list[tuple[Job, str, str]] = []
    pass_no = int(time.time() // 300)  # a 5-min slot number: rotates Workday full sweeps across boards
    ats_of: dict[str, str] = {}

    with client() as c:
        for i, b in enumerate(load_boards(names)):
            name = b["name"]
            ats_of[name] = kind(b)
            # health.json changes only when a board starts/stops failing, so healthy runs commit nothing.
            h = health.setdefault(name, {"ok": True, "fails": 0})
            bs = seen["boards"].setdefault(name, {"jobs": {}})
            known = bs["jobs"]
            baseline = "baseline_at" not in bs
            full = baseline or (pass_no - i) % FULL_SWEEP_EVERY == 0
            skip = {k for k, e in known.items() if e.get("status") != "pending"}
            try:
                jobs = FETCHERS[kind(b)](c, b, skip=skip, location_ok=pf.location_ok, full=full)
            except Exception as e:
                h.update(ok=False, error=f"{type(e).__name__}: {str(e)[:150]}", fails=h.get("fails", 0) + 1)
                h.setdefault("failing_since", ts)
                print(f"{name:<14} UNKNOWN (fetch failed: {h['error']})")
                if h["fails"] == cfg["board_fail_alert_after"]:
                    notify.push(f"jobwatch: {name} unreachable", f"{h['fails']} runs failed. Status unknown.\n{h['error']}",
                                tags=["warning"], dry_run=dry_run)
                continue

            new = [j for j in jobs if j.id not in known]
            retry = [j for j in jobs if known.get(j.id, {}).get("status") == "pending"]
            passed = 0
            for j in new:
                ok, why = pf.check(j)
                known[j.id] = {"first_seen": ts, "status": "baseline" if baseline else ("pending" if ok else "filtered")}
                if j.req_id:
                    known[j.id]["req"] = j.req_id
                if ok and not baseline:
                    passed += 1
                    alerts.append((j, why, known[j.id]["first_seen"]))
            for j in retry:
                alerts.append((j, pf.check(j)[1], known[j.id]["first_seen"]))
            if baseline:
                bs["baseline_at"] = ts
            if not h.get("ok", True) and h.get("fails", 0) >= cfg["board_fail_alert_after"]:
                notify.push(f"jobwatch: {name} back", f"Reachable again after {h['fails']} failed runs.", dry_run=dry_run)
            health[name] = {"ok": True, "fails": 0}
            print(f"{name:<14} fetched={len(jobs):<4} new={len(new):<4} passed={passed:<3} "
                  + ("(baseline: recorded, no alerts)" if baseline else ""))

        ready: list[tuple[Job, str, str, dict]] = []
        for job, why, first_seen in alerts:
            entry = seen["boards"][job.board]["jobs"][job.id]
            hooks = detail_hooks(ats_of.get(job.board, ""))
            if hooks:
                enrich, check_live = hooks
                try:  # stated dates and every location; the location check is redone with them
                    enrich(c, job)
                except Exception as e:
                    ok, live_why = False, f"detail API {type(e).__name__}: {str(e)[:100]}"
                else:
                    ok, why = pf.check(job)
                    if not ok:
                        entry["status"] = "filtered"
                        print(f"  filtered after detail: {job.board} {job.id} {job.title!r}: {why}")
                        continue
                    ok, live_why = check_live(c, job)
            else:
                ok, live_why = liveness(c, job)
            if not ok:
                entry["tries"] = entry.get("tries", 0) + 1
                if entry["tries"] >= cfg["max_liveness_tries"]:
                    entry["status"] = "dead"
                print(f"  not live yet: {job.board} {job.id} {job.title!r}: {live_why} (try {entry['tries']})")
                continue
            ready.append((job, why, first_seen, entry))

        # Screen (laptop), then push best fit first, newest within a fit level. OUT roles are not pushed:
        # they go to the daily digest. A screening failure sends the plain alert with the reason.
        verdicts = screen.load_verdicts() if screen_roles else {}
        results = []
        for job, why, first_seen, entry in ready:
            v, note = None, "Screening pending."
            if screen_roles:
                key = f"{job.board}:{job.id}"
                v = verdicts.get(key)
                if v is None:
                    text, src = jd.get(c, job)
                    try:
                        v = screen.screen(job, text, src)
                        verdicts[key] = v
                    except screen.ScreenError as e:
                        note = f"Screening unavailable: {str(e)[:120]}."
                        print(f"  screening failed: {job.board} {job.id}: {e}")
            results.append((job, why, first_seen, entry, v, note))
        results.sort(key=lambda r: ((r[4] or {}).get("fit", 0), r[0].posted_at or "", r[2]), reverse=True)
        for job, why, first_seen, entry, v, note in results:
            if v and v["verdict"] == "OUT":
                print(f"  OUT (digest): {job.board} {job.title!r}: {v['out_reason'] or v['one_line']}")
                if not dry_run:
                    screen.queue_out(job, v)
                entry["status"] = "screened"
                entry.pop("tries", None)
                continue
            title, body = format_alert(job, first_seen, why, v, note)
            if v and not dry_run:
                print(f"  kit: {screen.save_kit(job, v)}")
            sent = notify.push(title, body, click=job.url, tags=["briefcase"], dry_run=dry_run,
                               priority=4 if v and v["verdict"] == "IN" else 3,
                               actions=[{"action": "view", "label": "Apply", "url": job.apply_url},
                                        {"action": "view", "label": "JD", "url": job.url}])
            if sent:
                entry["status"] = "alerted"
                entry["alerted_at"] = state.now_iso()
                entry.pop("tries", None)
        if screen_roles and not dry_run:
            screen.save_verdicts(verdicts)

    if dry_run:
        print("dry-run: state not written")
    else:
        state.save_seen(seen)
        state.save_health(health)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="append")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--screen", action=argparse.BooleanOptionalAction, default=None,
                    help="screen hits with the Claude Code CLI before alerting (default: JOBWATCH_SCREEN=1)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    return run(args.board, args.dry_run, args.screen)


def load_env() -> None:
    """Local runs read private/ntfy.env; Actions passes NTFY_TOPIC as a secret."""
    import os
    env_file = ROOT / "private" / "ntfy.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() and v.strip():
                os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    sys.exit(main())
