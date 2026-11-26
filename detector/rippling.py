"""Rippling's own ATS (ats.rippling.com). Config: boards.yaml `endpoint` = https://ats.rippling.com/api/v2/board/<slug>/jobs.

Facts this relies on (checked live on Rippling, 2026-11-26):
- `GET .../jobs?page=0&pageSize=1000` returns every posting worldwide in one call (739), with
  `id`, `name`, `url` and `locations` (`countryCode`, `name`). There is no country filter (`country`
  is ignored) and no date, so India is kept here and alerts say "first seen".
- There is no public detail API (`.../jobs/<id>` 404s for open postings too). The job page
  (`ats.rippling.com/<slug>/jobs/<id>`) embeds the posting in __NEXT_DATA__ (`apiData.jobPost`,
  with `description.company` / `description.role` HTML and `workLocations`). `jobPost.createdOn` is the
  record's creation time, not a stated posting date, so it is not used.
- A closed or unknown posting's job and apply pages answer 200 after redirecting to
  www.rippling.com/careers/open-roles, so liveness requires the final URL to still be the job's own page.
"""

from __future__ import annotations

import json
import re

import httpx

from detector.sources import Job

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class RipplingError(RuntimeError):
    pass


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    r = c.get(board["endpoint"], params={"page": 0, "pageSize": 1000})
    r.raise_for_status()
    d = r.json()
    items = d.get("items") or []
    if d.get("totalItems", 0) > len(items):
        raise RipplingError(f"board lists {d['totalItems']} postings but one page returned {len(items)}")
    out = []
    for j in items:
        locs = [l for l in j.get("locations") or [] if l.get("countryCode") == "IN"]
        if not locs:
            continue
        url = j.get("url") or ""
        out.append(Job(
            board=board["name"], id=str(j["id"]), title=j["name"].strip(),
            locations=[l.get("name") or l.get("city") or "India" for l in locs],
            url=url, apply_url=f"{url}/apply", posted_at=None,
        ))
    return out


def _own_page(job: Job, r: httpx.Response) -> bool:
    return r.status_code == 200 and job.id in str(r.url)


def enrich(c: httpx.Client, job: Job) -> None:
    """JD and locations from the job page. Raises when it redirects away (closed or unknown)."""
    r = c.get(job.url)
    if not _own_page(job, r):
        raise RipplingError(f"job page HTTP {r.status_code} at {r.url}")
    m = NEXT_DATA.search(r.text)
    p = ((json.loads(m.group(1))["props"]["pageProps"].get("apiData") or {}).get("jobPost") or {}) if m else {}
    if not p:
        raise RipplingError("job page has no posting data")
    job.locations = list(p.get("workLocations") or job.locations)
    from detector.jd import html_to_text
    desc = p.get("description") or {}
    text = "\n\n".join(t for t in (html_to_text(desc.get("role")), html_to_text(desc.get("company"))) if t)
    job.jd = text or job.jd


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    try:
        r = c.get(job.apply_url)
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}"
    if not _own_page(job, r):
        return False, f"apply redirected to {r.url} (HTTP {r.status_code})"
    return True, "job page has the posting; apply page loads"
