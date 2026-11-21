"""Workday CXS adapter: one generic fetcher, per-company config from boards.yaml (endpoint, india_facet).

Facts this relies on (checked live 2026-11-21):
- Workday's edge answers Python TLS clients with 429 from some IPs while curl gets 200, so every
  Workday call goes through the system `curl`, serialized and paced (throttling is per source IP
  across all tenants).
- `POST .../jobs` returns postings newest first (`postedOn`: "Posted Today", "Posted Yesterday", ...),
  20 per page, and `total` only on the offset-0 page. So a normal pass reads pages only until one
  has nothing unseen or pending; each board also gets a full sweep about once an hour as a safety net.
- Tenants filter India either by a country facet (`india_facet` in boards.yaml) or only by city
  location facets. For the latter, the allowed-city location values are read from each pass's first
  response, and North India cities are left out at the source.
- `GET .../wday/cxs/{tenant}/{site}{externalPath}` gives `startDate` (the stated posting date),
  `location` + `additionalLocations`, `canApply` and `externalUrl`; it 404s once a posting closes.
  The public JD page is a JS shell that returns 200 even for closed postings, so the detail API is
  the liveness authority.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from detector.sources import Job

PAGE = 20
PACE_S = 1.0               # seconds between Workday requests (all tenants share one IP budget)
FULL_SWEEP_EVERY = 12      # passes; at a 5-min cadence each board gets a full sweep about hourly
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_last_call = 0.0


class WorkdayError(RuntimeError):
    pass


def curl(url: str, body: dict | None = None) -> tuple[int, str, str]:
    """(status, final_url, text). POST JSON when body is given."""
    global _last_call
    if not shutil.which("curl"):
        raise WorkdayError("curl not found (Workday calls need it)")
    wait = PACE_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    args = ["curl", "-s", "-L", "--max-time", "30", "-w", "\n%{http_code} %{url_effective}",
            "-H", "Accept: application/json", "-H", "Accept-Language: en-US"]
    if body is not None:
        args += ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-"]
    p = subprocess.run(args + [url], input=json.dumps(body) if body is not None else None,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
                       creationflags=NO_WINDOW)
    _last_call = time.monotonic()
    text, _, tail = p.stdout.rpartition("\n")
    code, _, final = tail.partition(" ")
    return (int(code) if code.isdigit() else 0), final, text


def _json(url: str, body: dict | None = None) -> dict:
    code, _, text = curl(url, body)
    if code != 200:
        raise WorkdayError(f"HTTP {code} from {url.split('/wday/')[0]}")
    return json.loads(text)


def urls(endpoint: str) -> tuple[str, str]:
    """(cxs base, public base) from the boards.yaml endpoint .../wday/cxs/{tenant}/{site}/jobs."""
    parts = urlsplit(endpoint)
    _, _, tenant, site, _ = parts.path.strip("/").split("/")
    cxs = f"{parts.scheme}://{parts.netloc}/wday/cxs/{tenant}/{site}"
    if parts.netloc.endswith("myworkdaysite.com"):
        return cxs, f"{parts.scheme}://{parts.netloc}/recruiting/{tenant}/{site}"
    return cxs, f"{parts.scheme}://{parts.netloc}/{site}"


def city_facets(facets: list, location_ok) -> dict[str, list[str]]:
    """Location-facet values for allowed Indian cities: {facetParameter: [ids]}."""
    out: dict[str, list[str]] = {}

    def walk(values, param):
        for v in values or []:
            p = v.get("facetParameter") or param
            if "id" in v and location_ok(v.get("descriptor") or ""):
                out.setdefault(p, []).append(v["id"])
            walk(v.get("values"), p)

    for f in facets:
        if "location" in (f.get("facetParameter") or "").lower():
            walk(f.get("values"), f.get("facetParameter"))
    return out


def job_id(path: str) -> str:
    """Stable id: the requisition suffix of externalPath (survives title edits), else the path."""
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit("_", 1)[-1] if "_" in tail else path


def fetch(board: dict, skip: set[str], location_ok, full: bool) -> list[Job]:
    """skip = IDs needing no further look (seen and not pending). full = enumerate every page."""
    name = board["name"]
    cxs, public = urls(board["endpoint"])
    if board.get("india_facet"):
        facets = {board["india_facet"]["param"]: [board["india_facet"]["id"]]}
    else:
        first = _json(f"{cxs}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        facets = city_facets(first.get("facets") or [], location_ok)
        if not facets:
            raise WorkdayError("no India location facet found")
    out, offset, total = [], 0, None
    while True:
        d = _json(f"{cxs}/jobs", {"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": ""})
        if total is None:
            total = d.get("total") or 0
        page = d.get("jobPostings") or []
        fresh = 0
        for p in page:
            path = p["externalPath"]
            jid = job_id(path)
            loc = p.get("locationsText") or ""
            out.append(Job(
                board=name, id=jid, title=p["title"].strip(),
                locations=[] if re.fullmatch(r"\d+ Locations", loc) else [loc],  # detail fills multi-location
                url=f"{public}{path}", apply_url=f"{public}{path}/apply", posted_at=None,
                req_id=(p.get("bulletFields") or [None])[0], check_urls=[f"{cxs}{path}"],
            ))
            fresh += jid not in skip
        offset += len(page)
        if not page or offset >= total or (not full and fresh == 0):
            return out


def enrich(job: Job) -> None:
    """Detail API: stated posting date, every location, canonical URLs. Raises if the posting is gone."""
    info = _json(job.check_urls[0])["jobPostingInfo"]
    job.posted_at = info.get("startDate") or job.posted_at
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    job.locations = [l for l in locs if l] or job.locations
    if info.get("externalUrl"):
        job.url, job.apply_url = info["externalUrl"], info["externalUrl"] + "/apply"
    job.open = bool(info.get("canApply", True))


def live(job: Job) -> tuple[bool, str]:
    if not getattr(job, "open", True):
        return False, "detail API says canApply=false"
    for label, url in (("JD", job.url), ("apply", job.apply_url)):
        code, final, _ = curl(url)
        if code != 200:
            return False, f"{label} HTTP {code}"
    return True, "detail API open; JD and apply pages load"
