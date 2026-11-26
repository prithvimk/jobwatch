"""Phenom adapter against recorded response shapes (Franklin Templeton, 2026-11-26). Workday calls go
through a fake curl, as in test_workday.py."""
import json

import httpx
import pytest

from detector import phenom, workday

BOARD = {"name": "Franklin Templeton", "endpoint": "https://careers.franklintempleton.com/us/en"}
WD = "https://franklintempleton.wd5.myworkdayjobs.com/Primary-External-1"
CXS = "https://franklintempleton.wd5.myworkdayjobs.com/wday/cxs/franklintempleton/Primary-External-1"


def row(i, country="India", apply=None):
    jid = str(869747 + i)
    return {"jobId": jid, "reqId": jid, "title": "Applied Scientist", "country": country,
            "multi_location": ["Hyderabad, Telangāna, India"], "cityStateCountry": "Hyderabad, Telangāna, India",
            "postedDate": "2026-11-18T00:00:00.000+0000",
            "applyUrl": apply or f"{WD}/job/Hyderabad-Telangana-India/Applied-Scientist_{jid}/apply"}


def client(rows, total=None, job_status=200):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/widgets":
            b = json.loads(req.content)
            assert b["ddoKey"] == "refineSearch" and b["selected_fields"] == {"country": ["India"]}
            page = rows[b["from"]:b["from"] + b["size"]]
            return httpx.Response(200, json={"refineSearch": {"status": 200, "hits": len(page),
                                  "totalHits": len(rows) if total is None else total, "data": {"jobs": page}}})
        if req.url.path.startswith("/us/en/job/"):
            return httpx.Response(job_status, html="<html>job</html>")
        if req.url.host == "apply.example.com":
            return httpx.Response(200, html="<html><form>apply</form></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_pages_and_builds_workday_detail_url():
    c, calls = client([row(i) for i in range(130)])
    jobs = phenom.fetch(c, BOARD)
    assert len(jobs) == 130 and len(calls) == 2
    j = jobs[0]
    assert j.id == "869747" and j.req_id == "869747" and j.title == "Applied Scientist"
    assert j.url == "https://careers.franklintempleton.com/us/en/job/869747"
    assert j.locations == ["Hyderabad, Telangāna, India"] and j.posted_at is None
    assert j.check_urls == [f"{CXS}/job/Hyderabad-Telangana-India/Applied-Scientist_869747"]


def test_rows_outside_india_are_dropped():
    c, _ = client([row(0), row(1, country="Japan")])
    assert [j.id for j in phenom.fetch(c, BOARD)] == ["869747"]


def test_workday_detail_url_variants():
    assert phenom.workday_detail(f"{WD}/job/X/Y_1/apply") == f"{CXS}/job/X/Y_1"
    assert phenom.workday_detail("https://acme.wd1.myworkdayjobs.com/en-US/Ext/job/X/Y_2") == \
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Ext/job/X/Y_2"
    assert phenom.workday_detail("https://career.icims.com/jobs/1/login") is None


def test_not_json_is_an_error():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, html="<html>blocked</html>")))
    with pytest.raises(phenom.PhenomError):
        phenom.fetch(c, BOARD)


@pytest.fixture
def fake_workday(monkeypatch):
    state = {"open": True}
    detail = {"jobPostingInfo": {"startDate": "2026-11-18", "location": "Hyderabad, Telangana, India",
                                 "externalUrl": f"{WD}/job/Hyderabad-Telangana-India/Applied-Scientist_869747",
                                 "canApply": True, "jobDescription": "<p>3+ years of PyTorch</p>"}}

    def f(url, body=None):
        if not state["open"]:
            return 404, url, '{"httpStatus":404}'
        if "/wday/cxs/" in url:
            return 200, url, json.dumps(detail)
        return 200, url, "<html></html>"
    monkeypatch.setattr(workday, "curl", f)
    return state


def test_workday_backed_enrich_and_live(fake_workday):
    c, _ = client([row(0)])
    [j] = phenom.fetch(c, BOARD)
    phenom.enrich(c, j)
    assert j.posted_at == "2026-11-18" and j.jd == "3+ years of PyTorch"
    assert j.url == f"{WD}/job/Hyderabad-Telangana-India/Applied-Scientist_869747"
    assert j.apply_url == j.url + "/apply"
    assert phenom.live(c, j)[0]


def test_workday_backed_closed_raises(fake_workday):
    c, _ = client([row(0)])
    [j] = phenom.fetch(c, BOARD)
    fake_workday["open"] = False
    with pytest.raises(workday.WorkdayError, match="404"):
        phenom.enrich(c, j)


def test_other_backend_uses_phenom_job_page():
    c, _ = client([row(0, apply="https://apply.example.com/job/1")])
    [j] = phenom.fetch(c, BOARD)
    assert j.check_urls == []
    phenom.enrich(c, j)
    c2, _ = client([row(0, apply="https://apply.example.com/job/1")], job_status=410)
    with pytest.raises(phenom.PhenomError, match="410"):
        phenom.enrich(c2, j)
