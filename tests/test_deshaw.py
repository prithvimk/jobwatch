"""D. E. Shaw India adapter against recorded page shapes (2026-11-26)."""
import json

import httpx
import pytest

from detector import deshaw
from detector.prefilter import Prefilter

BOARD = {"name": "D. E. Shaw India", "endpoint": "https://www.deshawindia.com/careers"}


def row(jid, title, offices=("HYD", "BLR"), exploratory=False):
    names = {"HYD": "Hyderabad", "BLR": "Bengaluru", "GGM": "Gurugram"}
    office = [{"abbreviation": o, "name": names[o]} for o in offices]
    data = {"id": jid, "displayName": title, "jobUrl": f"Lead-Tech-GAITech-{jid}", "activeOnJobsListing": True,
            "validToDate": None, "jobDescription": {
                "websiteDescription": "Build LLM applications.", "responsibilities": "Ship RAG systems.",
                "peopleWeAreLookingForHtml": "<ul><li>3+ years of Python</li></ul>"},
            "jobMetadata": {"isExploratory": exploratory, "jobLocations": office}}
    return {"data": data, "id": jid, "displayName": title, "office": office}


def page(props):
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": props}})}</script></html>'


ROWS = [row(7360, "Lead, Tech (GAITech)"), row(2781, "All positions in Financial Operations", exploratory=True),
        row(9001, "Member, Tech  (Data)", offices=("GGM",))]


def client(rows=ROWS, job_open=True, active=True):
    def handler(req: httpx.Request):
        if req.url.path == "/careers":
            return httpx.Response(200, html=page({"regularJobs": rows, "jobsFetchingError": False}))
        if req.url.path.startswith("/careers/"):
            if not job_open:
                return httpx.Response(200, html=page({"redirectToCareers": True}))
            data = dict(rows[0]["data"], activeOnJobsListing=active, datePosted="Thu Nov 26 2026 11:23:13 GMT+0000")
            return httpx.Response(200, html=page({"jobData": data}))
        if req.url.host == "www.apply.deshawindia.com":
            return httpx.Response(200, html="<html><form>apply</form></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_skips_exploratory_and_reads_jd():
    jobs = deshaw.fetch(client(), BOARD)
    assert [j.id for j in jobs] == ["7360", "9001"]
    j = jobs[0]
    assert j.title == "Lead, Tech (GAITech)" and j.req_id == "7360" and j.posted_at is None
    assert j.locations == ["Hyderabad, India", "Bengaluru, India"]
    assert j.url == "https://www.deshawindia.com/careers/lead-tech-gaitech-7360"
    assert j.apply_url == deshaw.APPLY
    assert "LLM" in j.jd and "RAG" in j.jd and "3+ years of Python" in j.jd
    assert jobs[1].title == "Member, Tech (Data)"
    assert not Prefilter.load().location_ok(jobs[1].locations[0])  # Gurugram only


def test_fetch_error_flag_raises():
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, html=page({"regularJobs": [], "jobsFetchingError": True}))))
    with pytest.raises(deshaw.DEShawError, match="jobsFetchingError"):
        deshaw.fetch(c, BOARD)


def test_page_without_next_data_raises():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, html="<html>blocked</html>")))
    with pytest.raises(deshaw.DEShawError):
        deshaw.fetch(c, BOARD)


def test_open_job_is_live():
    c = client()
    j = deshaw.fetch(c, BOARD)[0]
    deshaw.enrich(c, j)
    assert j.posted_at is None  # datePosted is the render time, never used
    assert deshaw.live(c, j)[0]


def test_removed_job_redirects_to_careers():
    c = client(job_open=False)
    j = deshaw.fetch(c, BOARD)[0]
    with pytest.raises(deshaw.DEShawError, match="closed"):
        deshaw.enrich(c, j)


def test_inactive_job_is_not_live():
    c = client(active=False)
    j = deshaw.fetch(c, BOARD)[0]
    deshaw.enrich(c, j)
    ok, why = deshaw.live(c, j)
    assert not ok and "activeOnJobsListing" in why
