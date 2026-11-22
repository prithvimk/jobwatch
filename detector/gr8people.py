"""gr8 People career sites (Teradata). Config: boards.yaml `endpoint` = https://<host>/graphql.

Facts this relies on (checked live on Teradata, 2026-11-22):
- The careers site is a Next.js front end over a GraphQL API at /graphql. Without the `token` cookie the
  /jobs page sets, `searchJobPostings` answers UNAUTHENTICATED, so each pass loads /jobs first (the shared
  client keeps the cookie). Introspection is off; the fields used here are the ones the site queries.
- `searchJobPostings(first, start)` returns at most 100 per call, newest first by `postedOn` (the stated
  posting time), worldwide (~200). There is no country filter, so every pass reads all pages (2-3 calls)
  and keeps postings with an Indian place.
- The job page is /jobs/<key>/<slug> (a wrong slug redirects to the right one) and embeds the posting in
  __NEXT_DATA__ with `status` and `descriptionHTML`. A posting that is not open returns 404. Apply is
  /jobs/<key>/<slug>/apply, which leads to the site's own sign-in.
"""

from __future__ import annotations

import json
import re

import httpx

from detector.sources import Job

PAGE = 100
QUERY = """query searchJobs($first: Int, $start: Int) {
  searchJobs: searchJobPostings(first: $first, start: $start) {
    results { totalCount nodes { key number title status postedOn places { nodes { name } } } }
  }
}"""
INDIA = re.compile(r"\bindia\b", re.I)
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class Gr8Error(RuntimeError):
    pass


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def host_of(board: dict) -> str:
    return httpx.URL(board["endpoint"]).host


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    host = host_of(board)
    c.get(f"https://{host}/jobs").raise_for_status()  # sets the session token the API requires
    out, start = [], 0
    while True:
        r = c.post(board["endpoint"], json={"query": QUERY, "variables": {"first": PAGE, "start": start}})
        r.raise_for_status()
        d = r.json()
        if d.get("errors"):
            e = d["errors"][0]
            raise Gr8Error(f"graphql {(e.get('extensions') or {}).get('code', '')} {e.get('message', '')}".strip())
        res = d["data"]["searchJobs"]["results"]
        nodes = res.get("nodes") or []
        for n in nodes:
            places = [p["name"] for p in (n.get("places") or {}).get("nodes") or [] if p.get("name")]
            if n.get("status", "OPEN") != "OPEN" or not any(INDIA.search(p) for p in places):
                continue
            url = f"https://{host}/jobs/{n['key']}/{slug(n['title'])}"
            out.append(Job(
                board=board["name"], id=str(n["key"]), title=n["title"].strip(), locations=places,
                url=url, apply_url=f"{url}/apply", posted_at=n.get("postedOn"), req_id=n.get("number"),
            ))
        start += len(nodes)
        if not nodes or start >= res.get("totalCount", 0) or start >= 2000:
            return out


def enrich(c: httpx.Client, job: Job) -> None:
    """JD and places from the job page's embedded posting. Raises when the posting is gone or not open."""
    r = c.get(job.url)
    if r.status_code != 200:
        raise Gr8Error(f"job page HTTP {r.status_code}")
    m = NEXT_DATA.search(r.text)
    p = (json.loads(m.group(1))["props"]["pageProps"].get("job") or {}) if m else {}
    if not p:
        raise Gr8Error("job page has no posting data")
    if p.get("status") != "OPEN":
        raise Gr8Error(f"posting status {p.get('status')}")
    job.url = str(r.url)
    job.apply_url = f"{job.url}/apply"
    places = [x["name"] for x in (p.get("places") or {}).get("nodes") or [] if x.get("name")]
    job.locations = places or job.locations
    job.posted_at = p.get("postedOn") or job.posted_at
    from detector.jd import html_to_text
    job.jd = html_to_text(p.get("descriptionHTML")) or job.jd


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    from detector.run import live as page_live  # late import: run imports sources, which loads this module
    ok, why = page_live(c, job.apply_url)
    if not ok:
        return False, f"apply {why}"
    return True, "job page open (status OPEN); apply page loads"
