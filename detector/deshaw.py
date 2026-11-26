"""D. E. Shaw India's own careers site (www.deshawindia.com/careers, a Next.js site). Config: boards.yaml
`endpoint` = https://www.deshawindia.com/careers.

Facts this relies on (checked live, 2026-11-26):
- The careers page embeds every posting in __NEXT_DATA__ (`props.pageProps.regularJobs`, ~78, all in the
  India offices Hyderabad, Bengaluru and Gurugram), each with `id`, `displayName`, `office` and `data`
  (`jobUrl`, `jobMetadata.isExploratory`, `jobDescription` parts). The page is ~2.3 MB (~311 KB gzipped),
  one call per pass. `internalJobs` (campus and internal roles) is not watched.
- "All positions in <group>" rows are standing exploratory applications (`isExploratory`), not
  postings, so they are skipped.
- No posting date is stated: the job page's `jobData.datePosted` is the time the page was rendered, so
  alerts say "first seen". `validToDate` is null on every posting seen.
- The job page `/careers/<slug>-<id>` (a wrong slug redirects to the right one) embeds `jobData`; an
  unknown or removed id answers 200 with `redirectToCareers: true` and no `jobData`, so the job page
  decides liveness. The apply button is one general form for every role
  (www.apply.deshawindia.com/ApplicationPage1.html?entity=DESIS), so it only has to load.
"""

from __future__ import annotations

import json
import re

import httpx

from detector.sources import Job

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
APPLY = "https://www.apply.deshawindia.com/ApplicationPage1.html?entity=DESIS"


class DEShawError(RuntimeError):
    pass


def page_props(text: str) -> dict:
    m = NEXT_DATA.search(text)
    if not m:
        raise DEShawError("page has no __NEXT_DATA__")
    return (json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}


def jd_text(data: dict) -> str | None:
    from detector.jd import html_to_text
    d = data.get("jobDescription") or {}
    parts = []
    for k in ("websiteDescription", "responsibilities", "peopleWeAreLookingFor"):
        v = d.get(f"{k}Html") or d.get(k) or d.get(f"{k}Str")
        if v:
            parts.append(html_to_text(v))
    return "\n\n".join(p for p in parts if p) or None


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    root = board["endpoint"].rstrip("/")
    r = c.get(root)
    r.raise_for_status()
    pp = page_props(r.text)
    if pp.get("jobsFetchingError"):
        raise DEShawError("site reports jobsFetchingError")
    rows = pp.get("regularJobs")
    if not isinstance(rows, list):
        raise DEShawError("page carries no regularJobs")
    out = []
    for j in rows:
        data = j.get("data") or {}
        if (data.get("jobMetadata") or {}).get("isExploratory"):
            continue
        jid = str(j.get("id") or data.get("id") or "")
        title = (j.get("displayName") or data.get("displayName") or "").strip()
        if not jid or not title:
            continue
        slug = (data.get("jobUrl") or f"job-{jid}").lower()
        offices = j.get("office") or (data.get("jobMetadata") or {}).get("jobLocations") or []
        out.append(Job(
            board=board["name"], id=jid, title=" ".join(title.split()),
            locations=[f"{o['name']}, India" for o in offices if o.get("name")],
            url=f"{root}/{slug}", apply_url=APPLY, posted_at=None, req_id=jid, jd=jd_text(data),
        ))
    return out


def enrich(c: httpx.Client, job: Job) -> None:
    """The job page must still carry the posting; raises once it is gone."""
    r = c.get(job.url)
    if r.status_code != 200:
        raise DEShawError(f"job page HTTP {r.status_code}")
    pp = page_props(r.text)
    data = pp.get("jobData")
    if pp.get("redirectToCareers") or not data:
        raise DEShawError("job page has no posting (closed)")
    if data.get("activeOnJobsListing") is False:
        job.open = False
    job.url = str(r.url)
    job.jd = jd_text(data) or job.jd


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    if not job.open:
        return False, "job page says activeOnJobsListing=false"
    from detector.run import live as page_live
    ok, why = page_live(c, job.apply_url)
    return (True, "job page open; apply form loads") if ok else (False, f"apply {why}")
