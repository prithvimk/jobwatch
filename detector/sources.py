"""Per-ATS fetchers that normalize postings into `Job`. One function per ATS type in boards.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

UA = "jobwatch/0.1 (personal job alerts)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass
class Job:
    board: str              # company name as in boards.yaml
    id: str                 # ATS job id: the state key
    title: str
    locations: list[str]
    url: str                # public JD page
    apply_url: str          # application entry page
    posted_at: str | None   # only when the ATS states it; never inferred
    req_id: str | None = None   # employer requisition id when exposed (for repost matching in the ledger)
    check_urls: list[str] = field(default_factory=list)  # extra liveness URLs (e.g. ATS detail API)
    open: bool = True           # ATS says applications are open (set by detail enrichment)
    closes_at: str | None = None  # closing date, only when the ATS states it


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True)


def greenhouse(c: httpx.Client, board: str, slug: str) -> list[Job]:
    r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    r.raise_for_status()
    out = []
    for j in r.json()["jobs"]:
        jid = str(j["id"])
        page = f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}"
        out.append(Job(
            board=board, id=jid, title=j["title"].strip(),
            locations=[(j.get("location") or {}).get("name", "")],
            url=j.get("absolute_url") or page, apply_url=page,
            posted_at=j.get("first_published"), req_id=j.get("requisition_id"),
            check_urls=[f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}"],
        ))
    return out


def lever(c: httpx.Client, board: str, slug: str) -> list[Job]:
    r = c.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        out.append(Job(
            board=board, id=j["id"], title=j["text"].strip(), locations=[l for l in locs if l],
            url=j["hostedUrl"], apply_url=j.get("applyUrl") or j["hostedUrl"],
            posted_at=None,  # Lever's createdAt is the internal create time, not a stated posting date
        ))
    return out


def ashby(c: httpx.Client, board: str, slug: str) -> list[Job]:
    r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    r.raise_for_status()
    out = []
    for j in r.json()["jobs"]:
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if addr.get("addressCountry"):
            locs[0] = f"{locs[0]}, {addr['addressCountry']}"
        out.append(Job(
            board=board, id=j["id"], title=j["title"].strip(), locations=[l for l in locs if l],
            url=j["jobUrl"], apply_url=j.get("applyUrl") or j["jobUrl"], posted_at=j.get("publishedAt"),
        ))
    return out


def smartrecruiters(c: httpx.Client, board: str, slug: str) -> list[Job]:
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    out, offset = [], 0
    while True:  # paginate; country filter applied server-side
        r = c.get(base, params={"limit": 100, "offset": offset, "country": "in"})
        r.raise_for_status()
        d = r.json()
        for j in d["content"]:
            loc = (j.get("location") or {})
            full = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
            url = f"https://jobs.smartrecruiters.com/{slug}/{j['id']}"
            out.append(Job(
                board=board, id=str(j["id"]), title=j["name"].strip(), locations=[full],
                url=url, apply_url=url, posted_at=j.get("releasedDate"), req_id=j.get("refNumber"),
                check_urls=[f"{base}/{j['id']}"],
            ))
        offset += len(d["content"])
        if not d["content"] or offset >= d["totalFound"]:
            return out


def amazon(c: httpx.Client, board: str, skip: set[str] = frozenset(), full: bool = True) -> list[Job]:
    """amazon.jobs search.json, India only. Max 100 per page; `sort=recent` is newest first by posted date
    (only roughly within a day), so a normal pass stops at the first page with nothing unseen or pending."""
    import json as _json
    import time
    from datetime import datetime
    url = "https://www.amazon.jobs/en/search.json"
    out, offset = [], 0
    while True:
        r = c.get(url, params={"result_limit": 100, "offset": offset, "country": "IND", "sort": "recent"})
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise RuntimeError(f"amazon.jobs: {d['error']}")
        page = d.get("jobs") or []
        fresh = 0
        for j in page:
            locs = []
            for raw in j.get("locations") or []:
                try:
                    l = _json.loads(raw) if isinstance(raw, str) else raw
                    locs.append(", ".join(x for x in (l.get("normalizedCityName") or l.get("city"),
                                                      l.get("normalizedStateName"), l.get("normalizedCountryName")) if x))
                except (ValueError, AttributeError):
                    pass
            if not locs and j.get("city"):
                locs = [f"{j['city']}, India"]
            try:
                posted = datetime.strptime(j.get("posted_date", ""), "%B %d, %Y").date().isoformat()
            except ValueError:
                posted = None
            jid = str(j["id_icims"])
            out.append(Job(
                board=board, id=jid, title=j["title"].strip(), locations=locs,
                url=f"https://www.amazon.jobs{j['job_path']}", apply_url=j.get("url_next_step") or "",
                posted_at=posted, req_id=jid,
            ))
            fresh += jid not in skip
        offset += len(page)
        if not page or offset >= d.get("hits", 0) or (not full and fresh == 0):
            return out
        time.sleep(0.5)


def _workday(c: httpx.Client, b: dict, **ctx) -> list[Job]:
    from detector import workday  # Workday goes through curl, not httpx (see detector/workday.py)
    return workday.fetch(b, **ctx)


def _eightfold(c: httpx.Client, b: dict, **ctx) -> list[Job]:
    from detector import eightfold
    return eightfold.fetch(c, b, **ctx)


def _oracle(c: httpx.Client, b: dict, **ctx) -> list[Job]:
    from detector import oracle
    return oracle.fetch(c, b, **ctx)


# fetcher(client, board entry from boards.yaml, skip=set of settled IDs, location_ok=fn, full=bool).
# Incremental fetchers (Workday) use the context to stop paging early; the JSON boards return everything.
FETCHERS = {
    "greenhouse": lambda c, b, **_: greenhouse(c, b["name"], b["locator"][0]),
    "lever": lambda c, b, **_: lever(c, b["name"], b["locator"][0]),
    "ashby": lambda c, b, **_: ashby(c, b["name"], b["locator"][0]),
    "smartrecruiters": lambda c, b, **_: smartrecruiters(c, b["name"], b["locator"][0]),
    "workday": _workday,
    "oracle": _oracle,
    "amazon": lambda c, b, skip=frozenset(), full=True, **_: amazon(c, b["name"], skip, full),
    "eightfold": _eightfold,
}


def detail_hooks(ats: str):
    """(enrich, live) for ATSs whose list lacks something an alert needs, or whose JD page is a JS shell
    that loads even when closed. enrich(c, job) fills it in from the detail API and raises when the
    posting is gone; live(c, job) replaces the generic page check. None for the plain JSON boards."""
    if ats == "workday":
        from detector import workday
        return workday.enrich, workday.live
    if ats == "oracle":
        from detector import oracle
        return oracle.enrich, oracle.live
    if ats == "eightfold":
        from detector import eightfold
        return eightfold.enrich, eightfold.live
    return None


def enrich_smartrecruiters_urls(c: httpx.Client, job: Job) -> None:
    """SmartRecruiters exposes the canonical posting and apply URLs only on the detail endpoint."""
    if not job.check_urls:
        return
    r = c.get(job.check_urls[0])
    if r.is_success:
        d = r.json()
        job.url = d.get("postingUrl") or job.url
        job.apply_url = d.get("applyUrl") or job.apply_url
