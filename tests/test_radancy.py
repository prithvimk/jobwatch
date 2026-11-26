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


# --- Charles Schwab (recorded 2026-11-26): title in <h2>, location and requisition inside the link, iCIMS apply ---
SCHWAB = {"name": "Charles Schwab", "endpoint": "https://www.schwabjobs.com/search-jobs/results",
          "results_module": "Search Results"}
ICIMS = "https://career-ind-schwab.icims.com/jobs/124308/manager%2c-business-systems-analysis/login"


def schwab_item(i):
    jid = 98379876016 + i
    return f"""
                        <li>
                            <a href="/job/hyderabad/manager-business-systems-analysis/33995/{jid}" data-job-id="{jid}">
                                <h2>Manager, Business Systems Analysis</h2>
                                    <span class="job-location">Hyderabad, Telangana</span>
                                    <div><span class="job-req-id"><b>Requisition ID: </b> 2026-{124308 + i}</span></div>
                                <button type="button" class="js-save-job-btn" data-job-id="{jid}" data-org-id="33995">Save for Later</button>
                            </a>
                        </li>"""


SCHWAB_LD = {"@type": "JobPosting", "datePosted": "2026-11-25",
             "description": "<h2>Your opportunity</h2><p>Build ML models</p>",
             "qualifications": "<ul><li>3+ years of Python and statistics</li></ul>",
             "identifier": {"@type": "PropertyValue", "name": "Job ID", "value": "2026-124308"},
             "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Hyderabad", "addressRegion": "Telangana",
                                                            "addressCountry": "India"}}]}


def schwab_client(n, icims_status=200):
    def handler(req: httpx.Request):
        if req.url.path == "/search-jobs/results":
            assert req.url.params["SearchResultsModuleName"] == "Search Results"
            assert req.url.params["FacetFilters[0].ID"] == "1269750"
            return httpx.Response(200, json={"results": results([schwab_item(i) for i in range(n)], n)})
        if req.url.path.startswith("/job/"):
            return httpx.Response(200, html=f'<html><script type="application/ld+json">{json.dumps(SCHWAB_LD)}</script>'
                                            f'<a class="button job-apply top" data-apply-url="{ICIMS}" href="{ICIMS}">Apply</a></html>')
        if req.url.host == "career-ind-schwab.icims.com":
            return httpx.Response(icims_status, html="<html><body>iCIMS</body></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_schwab_rows_with_h2_title_and_requisition():
    jobs = radancy.fetch(schwab_client(3), SCHWAB)
    assert len(jobs) == 3
    j = jobs[0]
    assert j.id == "98379876016" and j.title == "Manager, Business Systems Analysis"
    assert j.locations == ["Hyderabad, Telangana"] and j.req_id == "2026-124308"
    assert j.url == "https://www.schwabjobs.com/job/hyderabad/manager-business-systems-analysis/33995/98379876016"


def test_moodys_default_module_name():
    assert radancy.params(1)["SearchResultsModuleName"] == "Section 6 - Search Results List"


def test_schwab_jd_includes_qualifications_and_icims_live():
    c = schwab_client(1)
    [j] = radancy.fetch(c, SCHWAB)
    radancy.enrich(c, j)
    assert j.posted_at == "2026-11-25" and j.req_id == "2026-124308"
    assert "Build ML models" in j.jd and "3+ years of Python" in j.jd
    assert j.apply_url == ICIMS
    assert radancy.live(c, j)[0]


def test_schwab_closed_icims_410_is_not_live():
    c = schwab_client(1, icims_status=410)
    [j] = radancy.fetch(c, SCHWAB)
    radancy.enrich(c, j)
    ok, why = radancy.live(c, j)
    assert not ok and "410" in why
