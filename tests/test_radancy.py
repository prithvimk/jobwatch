"""Radancy adapter against recorded response shapes (Moody's, 2026-11-22)."""
import json

import httpx
import pytest

from detector import radancy
from detector.prefilter import Prefilter

BOARD = {"name": "Moody's", "endpoint": "https://careers.moodys.com/en/search-jobs/results"}
SF = "https://career8.successfactors.com/sfcareer/jobreqcareer?jobId=15285&amp;company=MoodysProd"


def item(i, loc="Bengaluru, India"):
    jid = 101070714080 + i
    return f"""
    <li class="search-results-list__item">
      <div class="search-results-list__content">
        <h2 class="search-results-list__job-title">
          <a class="search-results-list__job-link" href="/en/job/bengaluru/sr-data-scientist/49841/{jid}" data-job-id="{jid}" id="job-{jid}">Sr Data Scientist &amp; ML</a>
        </h2>
        <ul class="search-results-list__job-info-list">
          <li class="search-results-list__job-info job-location"> {loc} </li>
        </ul>
      </div>
      <div class="search-results-list__saved-job-container">
        <button type="button" class="js-save-job-btn" data-job-id="{jid}" data-org-id="49841">Save</button>
      </div>
    </li>"""


def results(items, total):
    return f'<section id="search-results" data-total-results="{total}" data-total-pages="1"><ul>{"".join(items)}</ul></section>'


LD = {"@context": "http://schema.org", "@type": "JobPosting", "datePosted": "2026-09-15",
      "description": "&lt;p&gt;3+ years of Python&lt;/p&gt;", "title": "Sr Data Scientist & ML",
      "identifier": {"@type": "PropertyValue", "name": "Job ID", "value": "15285"},
      "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Bengaluru", "addressRegion": "", "addressCountry": "India"}},
                      {"@type": "Place", "address": {"addressLocality": "Noida", "addressRegion": "", "addressCountry": "India"}}]}


def job_page():
    return (f'<html><script type="application/ld+json">{json.dumps(LD)}</script>'
            f'<a class="job-apply top" data-apply-url="{SF}" href="{SF}">Apply now</a></html>')


def client(n, per_page=100, job=True, sf_open=True):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/en/search-jobs/results":
            p = req.url.params
            assert p["FacetFilters[0].ID"] == "1269750" and p["FacetFilters[0].FacetType"] == "2"
            s = (int(p["CurrentPage"]) - 1) * per_page
            return httpx.Response(200, json={"filters": "", "results": results([item(i) for i in range(s, min(s + per_page, n))], n),
                                             "hasJobs": True})
        if req.url.path.startswith("/en/job/"):
            return httpx.Response(200, html=job_page()) if job else httpx.Response(404, html="<title>Job Error</title>")
        if req.url.host == "career8.successfactors.com":
            t = "Career Opportunities: Sr Data Scientist (15285)" if sf_open else "Career Opportunities"
            return httpx.Response(200, html=f"<html><head><title>{t} </title></head></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_parses_rows_and_pages():
    c, calls = client(130)
    jobs = radancy.fetch(c, BOARD)
    assert len(jobs) == 130 and len(calls) == 2
    j = jobs[0]
    assert j.id == "101070714080" and j.title == "Sr Data Scientist & ML" and j.posted_at is None
    assert j.url == "https://careers.moodys.com/en/job/bengaluru/sr-data-scientist/49841/101070714080"
    assert j.locations == ["Bengaluru, India"] and Prefilter.load().location_ok(j.locations[0])


def test_multi_location_row():
    [j] = radancy.parse_results(results([item(0, "Bengaluru, India; Noida, India")], 1), "https://x", "Moody's")
    assert j.locations == ["Bengaluru, India", "Noida, India"]


def test_not_json_is_an_error():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, html="<html>blocked</html>")))
    with pytest.raises(radancy.RadancyError):
        radancy.fetch(c, BOARD)


def test_enrich_reads_json_ld_and_successfactors_live():
    c, _ = client(1)
    [j] = radancy.fetch(c, BOARD)
    radancy.enrich(c, j)
    assert j.posted_at == "2026-09-15" and j.req_id == "15285"
    assert j.locations == ["Bengaluru, India", "Noida, India"]
    assert j.jd == "3+ years of Python"
    assert j.apply_url == "https://career8.successfactors.com/sfcareer/jobreqcareer?jobId=15285&company=MoodysProd"
    assert radancy.live(c, j)[0]


def test_closed_job_page_raises():
    c, _ = client(1, job=False)
    [j] = radancy.fetch(c, BOARD)
    with pytest.raises(radancy.RadancyError, match="404"):
        radancy.enrich(c, j)


def test_closed_successfactors_requisition_is_not_live():
    c, _ = client(1, sf_open=False)
    [j] = radancy.fetch(c, BOARD)
    radancy.enrich(c, j)
    ok, why = radancy.live(c, j)
    assert not ok and "closed" in why
