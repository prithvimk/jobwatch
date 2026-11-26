"""SAP SuccessFactors Career Site Builder sites (Nomura). Config: boards.yaml `endpoint` = https://<host>/<site>/search/.

Facts this relies on (checked live on Nomura, careers.nomura.com/Nomura, 2026-11-26):
- `<site>/search/?q=&locationsearch=India&startrow=N` is server-rendered HTML: 100 `tr.data-row` rows per page
  (title link `/<site>/job/<slug>/<id>/`, `span.jobLocation` like "Mumbai, IN") and "Results 1 – 100 of <total>".
  Nomura India is ~300 postings, so every pass reads all pages (3 calls). Rows carry no date. The default
  (relevance) order shifts between pages (300 rows held 237 distinct ids), so pages are sorted by
  `referencedate` desc, which pages cleanly.
- The job page states `<meta itemprop="datePosted" content="Mon Nov 09 08:00:00 UTC 2026">` (used as a date) and
  holds the JD in `span.jobdescription`. An unknown or closed id redirects to `/errorpage/?errortype=...`.
- The apply link (`/talentcommunity/apply/<id>/`) redirects non-browser clients to the careers home page for any
  id, so it proves nothing: the job page decides liveness, and apply starts from it.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from detector.sources import Job

PAGE = 100
ROW = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
LINK = re.compile(r'<a href="(/[^"]+/job/[^"]+/(\d+)/)" class="jobTitle-link">([^<]+)</a>')
PLACE = re.compile(r'<span class="jobLocation">\s*([^<]*?)\s*</span>')
TOTAL = re.compile(r"of\s*<b>(\d+)</b>")
POSTED = re.compile(r'itemprop="datePosted" content="([^"]+)"')
SPAN = re.compile(r"<(/?)span\b")


class CsbError(RuntimeError):
    pass


def fetch(c: httpx.Client, board: dict, **_) -> list[Job]:
    u = urlsplit(board["endpoint"])
    host = f"{u.scheme}://{u.netloc}"
    out, seen, start = [], set(), 0
    while True:
        r = c.get(board["endpoint"], params={"q": "", "locationsearch": "India", "startrow": start,
                                             "sortColumn": "referencedate", "sortDirection": "desc"})
        r.raise_for_status()
        rows = ROW.findall(r.text)
        for row in rows:
            m = LINK.search(row)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            place = PLACE.search(row)
            out.append(Job(
                board=board["name"], id=m.group(2), title=html.unescape(m.group(3)).strip(),
                locations=[html.unescape(place.group(1))] if place and place.group(1) else [],
                url=host + html.unescape(m.group(1)), apply_url=host + html.unescape(m.group(1)), posted_at=None,
            ))
        total = TOTAL.search(r.text)
        start += PAGE
        if not rows or not total or start >= int(total.group(1)) or start >= 3000:
            return out


def _description(page: str) -> str:
    i = page.find('class="jobdescription"')
    if i < 0:
        return ""
    pos, depth = page.rfind("<span", 0, i), 0
    for m in SPAN.finditer(page, pos):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            from detector.jd import html_to_text
            return html_to_text(page[pos:m.end() + 1])
    return ""


def enrich(c: httpx.Client, job: Job) -> None:
    """Stated posting date and JD from the job page. Raises when it redirects to the error page."""
    r = c.get(job.url)
    if r.status_code != 200 or "errorpage" in str(r.url) or 'class="jobdescription"' not in r.text:
        raise CsbError(f"job page HTTP {r.status_code} at {r.url}")
    if m := POSTED.search(r.text):
        try:
            job.posted_at = datetime.strptime(m.group(1), "%a %b %d %H:%M:%S UTC %Y").date().isoformat()
        except ValueError:
            pass
    job.jd = _description(r.text) or job.jd


def live(c: httpx.Client, job: Job) -> tuple[bool, str]:
    return True, "job page holds the posting (apply starts there; the apply link redirects bots for any id)"
