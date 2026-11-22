"""Radancy (TalentBrew) career sites (Moody's). Config: boards.yaml `endpoint` = https://<host>/<lang>/search-jobs/results.

Facts this relies on (checked live on Moody's, 2026-11-22):
- `search-jobs/results` (the site's own AJAX call) returns JSON whose `results` is an HTML list. With the
  country facet applied (type 2, id 1269750: the GeoNames id for India, which Radancy uses for its country
  facets) and RecordsPerPage=100, Moody's India (~50) is one call, so every pass reads it in full.
  List rows carry the Radancy job id, title, job page link and location text, but no date.
- The job page states the posting in JSON-LD: `datePosted` (a date, no time), `identifier` (the backend
  ATS's Job ID, used as the requisition id), every location and the JD. A closed or unknown job page
  returns 404.
- The apply button (`data-apply-url`) goes to the backend ATS. Moody's backend is SuccessFactors
  (`career8.successfactors.com/sfcareer/jobreqcareer?jobId=<Job ID>`), which answers 200 for closed
  requisitions too, with a "no longer available" page whose title lacks the job id; an open one is titled
  "Career Opportunities: <title> (<Job ID>)". So the apply check looks for the id in the title.
"""

from __future__ import annotations

import html
import json
import re

import httpx

from detector.sources import Job

INDIA_FACET = "1269750"
PER_PAGE = 100
ITEM = re.compile(r'<a[^>]*href="(?P<href>/[^"]+)"[^>]*data-job-id="(?P<id>\d+)"[^>]*>(?P<title>[^<]+)</a>(?P<rest>.*?)(?=<a[^>]*data-job-id="\d+"[^>]*>[^<]|\Z)', re.S)
LOCATION = re.compile(r'job-location[^"]*">([^<]*)<')
TOTAL = re.compile(r'data-total-results="(\d+)"')
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
APPLY = re.compile(r'data-apply-url="([^"]+)"')
TITLE = re.compile(r"<title>([^<]*)", re.I)


class RadancyError(RuntimeError):
    pass


def params(page: int) -> dict:
    return {
        "ActiveFacetID": INDIA_FACET, "CurrentPage": page, "RecordsPerPage": PER_PAGE, "Distance": 50,
        "RadiusUnitType": 0, "Keywords": "", "Location": "", "ShowRadius": "False", "IsPagination": "False",
        "FacetTerm": "", "FacetType": 0, "FacetFilters[0].ID": INDIA_FACET, "FacetFilters[0].FacetType": 2,
        "FacetFilters[0].Count": 0, "FacetFilters[0].Display": "India", "FacetFilters[0].IsApplied": "true",
        "FacetFilters[0].FieldName": "", "SearchResultsModuleName": "Section 6 - Search Results List",
        "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0, "SearchType": 5,
        "ResultsType": 0,
    }


def parse_results(fragment: str, base: str, board: str) -> list[Job]:
    out = []
    for m in ITEM.finditer(fragment):
        loc = LOCATION.search(m.group("rest"))
        locs = [l.strip() for l in html.unescape(loc.group(1)).split(";")] if loc else []
        url = base + html.unescape(m.group("href"))
        out.append(Job(
            board=board, id=m.group("id"), title=html.unescape(m.group("title")).strip(),
            locations=[l for l in locs if l], url=url, apply_url=url, posted_at=None,
        ))
    return out


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    u = httpx.URL(board["endpoint"])
    base = f"{u.scheme}://{u.host}"
    out, seen, page = [], set(), 1
    while True:
        r = c.get(board["endpoint"], params=params(page), headers={"X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", ""):
            raise RadancyError(f"results not JSON ({r.headers.get('content-type', '')})")
        frag = r.json().get("results") or ""
        total = TOTAL.search(frag)
        if not total:
            raise RadancyError("results carry no total count")
        rows = [j for j in parse_results(frag, base, board["name"]) if j.id not in seen]
        seen.update(j.id for j in rows)
        out += rows
        if not rows or len(out) >= int(total.group(1)) or page >= 20:
            return out
        page += 1


def posting(page_html: str) -> dict:
    for m in LD.finditer(page_html):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for item in d if isinstance(d, list) else [d]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def place(p: dict) -> str:
    a = (p or {}).get("address") or {}
    return ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"), a.get("addressCountry")) if x)


def enrich(c: httpx.Client, job: Job) -> None:
    """Stated date, requisition id, every location, JD and apply link from the job page.
    Raises when the page is gone (404) or carries no posting."""
    r = c.get(job.url)
    if r.status_code != 200:
        raise RadancyError(f"job page HTTP {r.status_code}")
    p = posting(r.text)
    if not p:
        raise RadancyError("job page has no JobPosting data")
    job.posted_at = p.get("datePosted") or job.posted_at
    ident = p.get("identifier")
    if isinstance(ident, dict) and ident.get("value"):
        job.req_id = str(ident["value"])
    places = p.get("jobLocation") or []
    locs = [place(x) for x in (places if isinstance(places, list) else [places])]
    job.locations = [l for l in locs if l] or job.locations
    from detector.jd import html_to_text
    job.jd = html_to_text(html.unescape(p.get("description") or "")) or job.jd
    a = APPLY.search(r.text)
    if a:
        job.apply_url = html.unescape(a.group(1))


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    """The job page answered in enrich(); this checks the backend apply page."""
    try:
        r = c.get(job.apply_url)
    except httpx.HTTPError as e:
        return False, f"apply {type(e).__name__}"
    if r.status_code >= 400:
        return False, f"apply HTTP {r.status_code}"
    if "successfactors" in str(r.url):
        t = TITLE.search(r.text)
        if not (job.req_id and t and f"({job.req_id})" in t.group(1)):
            return False, "apply SuccessFactors page does not show the requisition (closed)"
        return True, f"job page open; SuccessFactors requisition {job.req_id} open"
    from detector.run import live as page_live
    ok, why = page_live(c, job.apply_url)
    return (True, "job page open; apply page loads") if ok else (False, f"apply {why}")
