"""Oracle Recruiting Cloud (HCM Candidate Experience) adapter. Per-company config: locator = [host, siteNumber].

Facts this relies on (checked live on JPMC, 2026-11-21):
- `recruitingCEJobRequisitions` with `facetsList=LOCATIONS` lists country facets with ids; filtering by
  `locationId=<India id>` returns India postings only. Pages of 200 work, so a full fetch is 1-2 calls
  and runs every pass (no incremental paging needed).
- Each row states `PostedDate` (a date) and carries `PrimaryLocation` + `secondaryLocations[].Name`.
- `recruitingCEJobRequisitionDetails` (finder ById) gives `ExternalPostedStartDate` (exact time) and
  `ExternalPostedEndDate` (closing date). For a closed or unknown id it returns 200 with no items.
  The public job page is a JS shell that loads (200) for any id, so the detail API decides liveness.
"""

from __future__ import annotations

import httpx

from detector.sources import Job

PAGE = 200
API = "https://{host}/hcmRestApi/resources/latest"


class OracleError(RuntimeError):
    pass


def _get(c: httpx.Client, url: str, finder: str, expand: str) -> dict:
    r = c.get(url, params={"onlyData": "true", "expand": expand, "finder": finder})
    r.raise_for_status()
    return r.json()


def country_id(c: httpx.Client, host: str, site: str, country: str = "India") -> int:
    d = _get(c, f"{API.format(host=host)}/recruitingCEJobRequisitions",
             f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit=1", "requisitionList")
    for f in d["items"][0].get("locationsFacet") or []:
        if f.get("Name") == country:
            return f["Id"]
    raise OracleError(f"no {country} location facet")


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    host, site = board["locator"][0], board["locator"][1]
    loc_id = country_id(c, host, site)
    base = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job"
    out, offset = [], 0
    while True:
        d = _get(c, f"{API.format(host=host)}/recruitingCEJobRequisitions",
                 f"findReqs;siteNumber={site},limit={PAGE},offset={offset},locationId={loc_id},sortBy=POSTING_DATES_DESC",
                 "requisitionList.secondaryLocations")
        item = d["items"][0]
        rows = item.get("requisitionList") or []
        for r in rows:
            jid = str(r["Id"])
            locs = [r.get("PrimaryLocation")] + [s.get("Name") for s in r.get("secondaryLocations") or []]
            out.append(Job(
                board=board["name"], id=jid, title=r["Title"].strip(), locations=[l for l in locs if l],
                url=f"{base}/{jid}", apply_url=f"{base}/{jid}/apply/email", posted_at=r.get("PostedDate"),
                req_id=jid,
                check_urls=[f"{API.format(host=host)}/recruitingCEJobRequisitionDetails"
                            f"?onlyData=true&finder=ById;Id=%22{jid}%22,siteNumber={site}"],
            ))
        offset += len(rows)
        if not rows or offset >= item.get("TotalJobsCount", 0):
            return out


def enrich(c: httpx.Client, job: Job) -> None:
    """Exact posting time and closing date. Raises if the posting is closed or unknown."""
    r = c.get(job.check_urls[0])
    r.raise_for_status()
    items = r.json().get("items") or []
    if not items:
        raise OracleError("detail API has no such open posting")
    d = items[0]
    job.posted_at = d.get("ExternalPostedStartDate") or job.posted_at
    job.closes_at = d.get("ExternalPostedEndDate")
    from detector.jd import sections
    job.jd = sections(("", d.get("ExternalDescriptionStr")), ("Responsibilities", d.get("ExternalResponsibilitiesStr")),
                      ("Qualifications", d.get("ExternalQualificationsStr"))) or job.jd


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    for label, url in (("JD", job.url), ("apply", job.apply_url)):
        try:
            code = c.get(url).status_code
        except httpx.HTTPError as e:
            return False, f"{label} {type(e).__name__}"
        if code != 200:
            return False, f"{label} HTTP {code}"
    return True, "detail API open; JD and apply pages load"
