"""Eightfold PCSX adapter (Microsoft). Config: boards.yaml `endpoint` = https://<host>/api/pcsx/search?domain=<domain>.

Facts this relies on (checked live on Microsoft, 2026-11-21):
- The first request from a fresh client gets 429 "Please try again later" and sets `_vs*` cookies;
  the same client then gets 200. Bursts also get 429. So: one client per pass (cookies kept), 3 s
  between calls, and one retry on 429.
- `/api/pcsx/search?location=India&sort_by=timestamp` is newest first, 10 per page (`num` is ignored),
  with `postedTs` (epoch seconds, the stated posting time). A normal pass stops at the first page
  with nothing unseen or pending, like Workday.
- `/api/pcsx/position_details` returns 404 for a closed or unknown position, while the job page is a
  JS shell that loads for any id, so the details API decides liveness. Microsoft applies on Eightfold
  itself (`/careers/apply?pid=`). Some tenants hand off to another ATS (Morgan Stanley -> Workday);
  track those on the backend instead.

Older tenants (HSBC, checked 2026-11-25) answer "PCSX is not enabled" and serve the v2 API instead:
boards.yaml `endpoint` = https://<host>/api/apply/v2/jobs?domain=<domain>.
- `/api/apply/v2/jobs?location=India&sort_by=timestamp`: 10 per page (`num` ignored), "hot" postings
  pinned first, then newest first. Rows carry `t_create`/`t_update` (Eightfold's own record times) and
  `custom_data.postedDate` (null for HSBC); only a non-null postedDate is used as a stated date.
- `/api/apply/v2/jobs/<id>` gives the JD and `apply_redirect_url`; it and the job page 404 once closed.
- HSBC applies on SuccessFactors (`career2.successfactors.eu`), which answers 200 for any requisition,
  showing a sign-in page for unknown ones. An open one's title names the requisition "(<id>)".
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx

from detector.sources import Job

PACE_S = 3.0
_last: dict[str, float] = {}


class EightfoldError(RuntimeError):
    pass


def get(c: httpx.Client, url: str, **params) -> httpx.Response:
    host = httpx.URL(url).host
    for attempt in range(2):
        wait = PACE_S - (time.monotonic() - _last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        r = c.get(url, params=params or None)
        _last[host] = time.monotonic()
        if r.status_code != 429:
            return r
    return r  # still 429 after the cookie retry: caller reports it


def config(board: dict) -> tuple[str, str]:
    u = urlsplit(board["endpoint"])
    return u.netloc, parse_qs(u.query)["domain"][0]


def is_v2(board: dict) -> bool:
    return "/api/apply/v2/jobs" in board["endpoint"]


def fetch(c: httpx.Client, board: dict, skip: set[str] = frozenset(), full: bool = True, **_) -> list[Job]:
    if is_v2(board):
        return fetch_v2(c, board, skip=skip, full=full)
    host, domain = config(board)
    out, start = [], 0
    while True:
        r = get(c, f"https://{host}/api/pcsx/search", domain=domain, location="India", start=start, sort_by="timestamp")
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            raise EightfoldError(f"search HTTP {r.status_code}")
        d = r.json()["data"]
        page = d.get("positions") or []
        fresh = 0
        for p in page:
            pid = str(p["id"])
            ts = p.get("postedTs")
            out.append(Job(
                board=board["name"], id=pid, title=p["name"].strip(), locations=list(p.get("locations") or []),
                url=f"https://{host}/careers/job/{pid}",
                apply_url=f"https://{host}/careers/apply?pid={pid}&domain={domain}",
                posted_at=datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                req_id=p.get("displayJobId") or p.get("atsJobId"),
                check_urls=[f"https://{host}/api/pcsx/position_details?position_id={pid}&domain={domain}&hl=en"],
            ))
            fresh += pid not in skip
        start += len(page)
        if not page or start >= d.get("count", 0) or (not full and fresh == 0):
            return out


def fetch_v2(c: httpx.Client, board: dict, skip: set[str] = frozenset(), full: bool = True) -> list[Job]:
    host, domain = config(board)
    out, start = [], 0
    while True:
        r = get(c, f"https://{host}/api/apply/v2/jobs", domain=domain, location="India", start=start, sort_by="timestamp")
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            raise EightfoldError(f"v2 jobs HTTP {r.status_code}")
        d = r.json()
        page = d.get("positions") or []
        fresh = 0
        for p in page:
            pid = str(p["id"])
            posted = (p.get("custom_data") or {}).get("postedDate")
            out.append(Job(
                board=board["name"], id=pid, title=(p.get("posting_name") or p["name"]).strip(),
                locations=list(p.get("locations") or ([p["location"]] if p.get("location") else [])),
                url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{pid}",
                apply_url=f"https://{host}/careers/apply?pid={pid}&domain={domain}",
                posted_at=posted or None,
                req_id=p.get("display_job_id") or p.get("ats_job_id"),
                check_urls=[f"https://{host}/api/apply/v2/jobs/{pid}?domain={domain}"],
            ))
            fresh += pid not in skip
        start += len(page)
        if not page or start >= d.get("count", 0) or (not full and fresh == 0 and start > 10):
            return out  # the first page also holds pinned "hot" rows, so read at least two


def enrich(c: httpx.Client, job: Job) -> None:
    """Full location list from the details API. Raises when the position is closed or unknown."""
    if "/api/apply/v2/jobs/" in job.check_urls[0]:
        return enrich_v2(c, job)
    r = get(c, job.check_urls[0])
    if r.status_code != 200:
        raise EightfoldError(f"position_details HTTP {r.status_code}")
    d = r.json().get("data") or {}
    job.locations = list(d.get("locations") or job.locations)
    from detector.jd import html_to_text
    job.jd = html_to_text(d.get("jobDescription")) or job.jd
    apply = ((d.get("positionUserActions") or {}).get("applyAction") or {}).get("applyUrl")
    if apply:  # tenant hands off to another ATS
        job.apply_url = apply


def enrich_v2(c: httpx.Client, job: Job) -> None:
    r = get(c, job.check_urls[0])
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise EightfoldError(f"v2 job HTTP {r.status_code}")
    d = r.json()
    job.locations = list(d.get("locations") or job.locations)
    from detector.jd import html_to_text
    job.jd = html_to_text(d.get("job_description")) or job.jd
    if d.get("apply_redirect_url"):
        job.apply_url = d["apply_redirect_url"]


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    if "successfactors" in job.apply_url and job.req_id:
        try:
            jd = get(c, job.url).status_code
            r = get(c, job.apply_url)
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}"
        if jd != 200:
            return False, f"JD HTTP {jd}"
        title = re.search(r"<title>([^<]*)", r.text)  # ~300 KB into a ~600 KB page
        if r.status_code != 200 or not title or f"({job.req_id})" not in title.group(1):
            return False, f"SuccessFactors page does not name requisition {job.req_id}"
        return True, "details API open; JD loads; SuccessFactors names the requisition"
    for label, url in (("JD", job.url), ("apply", job.apply_url)):
        try:
            code = get(c, url).status_code
        except httpx.HTTPError as e:
            return False, f"{label} {type(e).__name__}"
        if code != 200:
            return False, f"{label} HTTP {code}"
    return True, "details API open; JD and apply pages load"
