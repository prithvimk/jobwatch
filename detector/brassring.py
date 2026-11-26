"""IBM Kenexa BrassRing (TG new UI) career sites (UBS). Config: boards.yaml `endpoint` =
https://<host>/TgNewUI/Search/Ajax/MatchedJobs?partnerid=<partner>&siteid=<site>.

Facts this relies on (checked live on UBS, jobs.ubs.com partner 25008 / site 5012, 2026-11-26):
- The search home page (`/TGnewUI/Search/Home/Home?partnerid=&siteid=`) sets session cookies and holds an
  anti-forgery token (`__RequestVerificationToken`), which the AJAX calls need as the `RFT` header.
- `POST /TgNewUI/Search/Ajax/MatchedJobs` returns the first 50 rows, `JobsCount` and `Facets`. Its `location`
  field does not filter. The city facet (UBS: `formtext2`, "City") does: `ProcessSortAndShowMoreJobs` with
  `facetfilterfields` selecting the allowed Indian cities read from the first response, paged by
  `pageNumber` (50 per page). UBS India is ~30 postings, so a pass is 2-3 calls.
- Rows are `Questions` lists (`reqid`, `jobtitle`, `formtext2` city, `formtext23` country, `lastupdated`).
  `lastupdated` is an edit date, not a stated posting date, so alerts say "first seen".
- The job page (`Link`, ...HomeWithPreLoad?...&PageType=JobDetails&jobid=<reqid>) embeds the posting in the
  hidden `preLoadJSON` input: `Jobdetails` has `isActive`, `AutoOptionalReqId` (the employer reference,
  e.g. 344740BR) and `JobDetailQuestions` (the JD sections). An unknown or closed job id answers 200 with
  `Jobdetails: null`, so that decides liveness. Applying starts from the same page.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from detector.sources import Job

TOKEN = re.compile(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"')
PRELOAD = re.compile(r'<input id="preLoadJSON" type="hidden" value="([^"]*)"')
LOC_FACET = re.compile(r"city|location|country", re.I)
SKIP_SECTIONS = re.compile(r"about us|how we hire|join us|contact|disclaimer|policy", re.I)
MAX_PAGES = 40


class BrassRingError(RuntimeError):
    pass


def config(board: dict) -> tuple[str, str, str]:
    """(base, partnerid, siteid) from the boards.yaml endpoint."""
    u = urlsplit(board["endpoint"])
    q = parse_qs(u.query)
    return f"{u.scheme}://{u.netloc}", q["partnerid"][0], q["siteid"][0]


def _q(row: dict, name: str) -> str:
    return next(((x.get("Value") or "").strip() for x in row.get("Questions") or [] if x.get("QuestionName") == name), "")


def _session(c: httpx.Client, base: str, partner: str, site: str) -> str:
    r = c.get(f"{base}/TGnewUI/Search/Home/Home", params={"partnerid": partner, "siteid": site})
    r.raise_for_status()
    m = TOKEN.search(r.text)
    if not m:
        raise BrassRingError("search page has no request token")
    return m.group(1)


def _body(partner: str, site: str, **extra) -> dict:
    return {"partnerId": partner, "siteId": site, "keyword": "", "location": "", "keywordCustomSolrFields": "",
            "locationCustomSolrFields": "", "linkId": "", "Latitude": 0, "Longitude": 0,
            "facetfilterfields": {"Facet": []}, "powersearchoptions": {"PowerSearchOption": []},
            "SortType": "LastUpdated", "encryptedSessionValue": "", **extra}


def city_filter(facets: list, location_ok) -> list[dict]:
    """Facet selections for the allowed Indian places in the location-like facets of a response."""
    out = []
    for f in facets:
        if not LOC_FACET.search(f"{f.get('Name', '')} {f.get('Description', '')}"):
            continue
        opts = [{"OptionName": o["OptionName"], "OptionValue": o["OptionValue"], "Selected": True}
                for o in f.get("Options") or [] if location_ok(o.get("OptionName") or "")]
        if opts:
            out.append({"Name": f["Name"], "Description": f.get("Description", ""), "Options": opts})
    return out


def fetch(c: httpx.Client, board: dict, location_ok=None, **_) -> list[Job]:
    from detector.prefilter import Prefilter
    location_ok = location_ok or Prefilter.load().location_ok
    base, partner, site = config(board)
    headers = {"RFT": _session(c, base, partner, site), "X-Requested-With": "XMLHttpRequest"}
    first = c.post(f"{base}/TgNewUI/Search/Ajax/MatchedJobs", json=_body(partner, site), headers=headers)
    first.raise_for_status()
    facets = city_filter(((first.json().get("Facets") or {}).get("Facet")) or [], location_ok)
    if not facets:
        return []  # no allowed Indian place on the board right now
    out: list[Job] = []
    for page in range(1, MAX_PAGES + 1):
        r = c.post(f"{base}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs", headers=headers,
                   json=_body(partner, site, facetfilterfields={"Facet": facets}, pageNumber=page))
        r.raise_for_status()
        d = r.json()
        rows = (d.get("Jobs") or {}).get("Job") or []
        for row in rows:
            rid = _q(row, "reqid")
            if not rid:
                continue
            place = ", ".join(x for x in (_q(row, "formtext2"), _q(row, "formtext23")) if x)
            link = row.get("Link") or (f"{base}/TGnewUI/Search/home/HomeWithPreLoad?partnerid={partner}"
                                       f"&siteid={site}&PageType=JobDetails&jobid={rid}")
            out.append(Job(
                board=board["name"], id=rid, title=html.unescape(_q(row, "jobtitle")), locations=[place] if place else [],
                url=link, apply_url=link, posted_at=None,
            ))
        if not rows or len(out) >= (d.get("JobsCount") or 0):
            return out
    return out


def details(c: httpx.Client, job: Job) -> dict:
    r = c.get(job.url)
    r.raise_for_status()
    m = PRELOAD.search(r.text)
    jd = (json.loads(html.unescape(m.group(1))).get("Jobdetails") if m else None) or None
    if not jd:
        raise BrassRingError("job page has no posting (closed or unknown)")
    return jd


def enrich(c: httpx.Client, job: Job) -> None:
    """Employer reference, place and JD from the job page. Raises when the posting is gone."""
    d = details(c, job)
    job.open = str(d.get("isActive", "True")).lower() == "true"
    job.req_id = d.get("AutoOptionalReqId") or job.req_id
    qs = d.get("JobDetailQuestions") or []
    city = next((q.get("AnswerValue") for q in qs if (q.get("QuestionName") or "").strip().lower() == "city"), "")
    if city and not any(city in l for l in job.locations):
        job.locations = [city] + job.locations
    from detector.jd import html_to_text
    parts = [f"{q['QuestionName']}\n{html_to_text(q.get('AnswerValue'))}" for q in qs
             if q.get("QuestionName") and len(q.get("AnswerValue") or "") > 60
             and not SKIP_SECTIONS.search(q["QuestionName"])]
    job.jd = "\n\n".join(parts) or job.jd


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    if not job.open:
        return False, "job page says the posting is not active"
    return True, "job page holds the open posting (apply starts there)"
