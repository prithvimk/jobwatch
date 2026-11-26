"""Phenom career sites (Franklin Templeton). Config: boards.yaml `endpoint` = https://<host>/<country>/<lang>
(the site's locale root, e.g. https://careers.franklintempleton.com/us/en).

Facts this relies on (checked live on Franklin Templeton, 2026-11-26):
- `POST https://<host>/widgets` with `ddoKey: refineSearch` and `selected_fields: {"country": ["India"]}`
  is the site's own search call. It needs no cookies or page id, returns JSON with `totalHits`, and takes
  `from`/`size` (100 per call), so Franklin Templeton's India (~113) is two calls, read in full every pass.
  `sortBy` is accepted but does not change the order.
- Each row has `jobId`, `reqId`, `title`, `multi_location` (else `cityStateCountry`), `applyUrl` and
  `postedDate`. `postedDate` is not used: for Workday apply links the Workday detail API's `startDate`
  is the stated date, as on every Workday board; other backends say "first seen".
- The job page is `<endpoint>/job/<jobId>`; an unknown or closed posting answers 410.
- Franklin Templeton's apply links all go to Workday (`franklintempleton.wd5 Primary-External-1`), a site
  whose CXS list returns nothing (hidden site) while its detail API answers per posting. So for Workday
  apply links the Workday detail API decides liveness and gives the date, every location and the JD
  (`workday.enrich` / `workday.live`). Other backends fall back to the Phenom job page and the generic
  page check.
"""

from __future__ import annotations

import re

import httpx

from detector.sources import Job

PER_PAGE = 100
WORKDAY_APPLY = re.compile(r"^(https://[^/]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+?)(?:/apply)?/?$")


class PhenomError(RuntimeError):
    pass


def body(frm: int, size: int = PER_PAGE) -> dict:
    return {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
            "ddoKey": "refineSearch", "from": frm, "size": size, "jobs": True, "counts": True,
            "all_fields": ["country"], "siteType": "external", "keywords": "", "global": True,
            "selected_fields": {"country": ["India"]}}


def workday_detail(apply_url: str) -> str | None:
    """Workday CXS detail URL for a Workday apply link, else None."""
    m = WORKDAY_APPLY.match(apply_url or "")
    if not m:
        return None
    host, site, path = m.groups()
    tenant = httpx.URL(host).host.split(".")[0]
    return f"{host}/wday/cxs/{tenant}/{site}{path}"


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    root = board["endpoint"].rstrip("/")
    u = httpx.URL(root)
    widgets = f"{u.scheme}://{u.host}/widgets"
    out, seen, frm = [], set(), 0
    while True:
        r = c.post(widgets, json=body(frm))
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", ""):
            raise PhenomError(f"search not JSON ({r.headers.get('content-type', '')})")
        d = r.json().get("refineSearch") or {}
        if "totalHits" not in d:
            raise PhenomError("search carries no totalHits")
        rows = (d.get("data") or {}).get("jobs") or []
        for j in rows:
            jid = str(j.get("jobId") or "")
            if not jid or jid in seen or not j.get("title"):
                continue
            if (j.get("country") or "India") != "India":
                continue
            seen.add(jid)
            locs = j.get("multi_location") or [j.get("cityStateCountry") or j.get("location") or ""]
            apply_url = j.get("applyUrl") or f"{root}/job/{jid}"
            wd = workday_detail(apply_url)
            out.append(Job(
                board=board["name"], id=jid, title=j["title"].strip(), locations=[l for l in locs if l],
                url=f"{root}/job/{jid}", apply_url=apply_url,
                posted_at=None,
                req_id=str(j["reqId"]) if j.get("reqId") else None, check_urls=[wd] if wd else [],
            ))
        frm += len(rows)
        if not rows or frm >= int(d["totalHits"]) or frm >= 2000:
            return out


def _workday(job: Job) -> bool:
    return bool(job.check_urls) and "/wday/cxs/" in job.check_urls[0]


def enrich(c: httpx.Client, job: Job) -> None:
    """Workday-backed: the Workday detail API (raises once closed). Otherwise the Phenom job page must load."""
    if _workday(job):
        from detector import workday
        workday.enrich(c, job)
        return
    r = c.get(job.url)
    if r.status_code != 200:
        raise PhenomError(f"job page HTTP {r.status_code}")


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    if _workday(job):
        from detector import workday
        return workday.live(c, job)
    from detector.run import live as page_live
    ok, why = page_live(c, job.apply_url)
    return (True, "job page open; apply page loads") if ok else (False, f"apply {why}")
