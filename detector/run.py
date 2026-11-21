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
from pathlib import Path

import httpx
import yaml

from detector import notify, state
from detector.prefilter import Prefilter
from detector.sources import FETCHERS, Job, client, enrich_smartrecruiters_urls

ROOT = Path(__file__).resolve().parent.parent
CLOSED_URL = ("error=true", "/404", "job-not-found", "jobnotfound")  # closed jobs redirect here
APPLY_ENTRY = ("/oneclick-ui/",)  # ATS application entry paths that refuse non-browser clients
CLOSED_TEXT = ("this job is no longer", "position has been filled", "no longer accepting applications",
               "job you are looking for is no longer")


def load_boards(names: list[str] | None) -> list[dict]:
    boards = yaml.safe_load((ROOT / "boards.yaml").read_text(encoding="utf-8"))["boards"]
    boards = [b for b in boards if b.get("ats") in FETCHERS]
    return [b for b in boards if b["name"] in names] if names else boards


def live(c: httpx.Client, url: str) -> tuple[bool, str]:
    """The page loads (2xx after redirects) and doesn't land on a closed/404 page."""
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


def format_alert(job: Job, first_seen: str, why: str) -> tuple[str, str]:
    title = f"{job.board}: {job.title}"
    when = (f"Posted {notify.ist(job.posted_at)} (per ATS)" if job.posted_at
            else f"First seen {notify.ist(first_seen)} (posting date not stated)")
    loc = "; ".join(l for l in job.locations if l) or "location unknown"
    body = f"{loc}\n{when}\nPrefilter: {why}. Screening pending."
    return title, body


def run(names: list[str] | None, dry_run: bool) -> int:
    pf = Prefilter.load()
    cfg = yaml.safe_load((Path(__file__).with_name("config.yaml")).read_text(encoding="utf-8"))
    seen, health = state.load_seen(), state.load_health()
    ts = state.now_iso()
    alerts: list[tuple[Job, str, str]] = []

    with client() as c:
        for b in load_boards(names):
            name, slug = b["name"], b["locator"][0]
            # health.json changes only when a board starts/stops failing, so healthy runs commit nothing.
            h = health.setdefault(name, {"ok": True, "fails": 0})
            try:
                jobs = FETCHERS[b["ats"]](c, name, slug)
            except Exception as e:
                h.update(ok=False, error=f"{type(e).__name__}: {str(e)[:150]}", fails=h.get("fails", 0) + 1)
                h.setdefault("failing_since", ts)
                print(f"{name:<14} UNKNOWN (fetch failed: {h['error']})")
                if h["fails"] == cfg["board_fail_alert_after"]:
                    notify.push(f"jobwatch: {name} unreachable", f"{h['fails']} runs failed. Status unknown.\n{h['error']}",
                                tags=["warning"], dry_run=dry_run)
                continue

            bs = seen["boards"].setdefault(name, {"jobs": {}})
            known = bs["jobs"]
            baseline = "baseline_at" not in bs
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

        # Newest stated posting first, then first-seen. (Fit ordering is the screener's job.)
        alerts.sort(key=lambda a: (a[0].posted_at or "", a[2]), reverse=True)
        for job, why, first_seen in alerts:
            entry = seen["boards"][job.board]["jobs"][job.id]
            ok, live_why = liveness(c, job)
            if not ok:
                entry["tries"] = entry.get("tries", 0) + 1
                if entry["tries"] >= cfg["max_liveness_tries"]:
                    entry["status"] = "dead"
                print(f"  not live yet: {job.board} {job.id} {job.title!r}: {live_why} (try {entry['tries']})")
                continue
            title, body = format_alert(job, first_seen, why)
            sent = notify.push(title, body, click=job.url, tags=["briefcase"], dry_run=dry_run,
                               actions=[{"action": "view", "label": "Apply", "url": job.apply_url},
                                        {"action": "view", "label": "JD", "url": job.url}])
            if sent:
                entry["status"] = "alerted"
                entry["alerted_at"] = state.now_iso()
                entry.pop("tries", None)

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
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    return run(args.board, args.dry_run)


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
