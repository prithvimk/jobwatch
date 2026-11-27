"""Eightfold PCSX adapter against recorded response shapes (Microsoft, 2026-11-21)."""
import httpx
import pytest

from detector import eightfold
from detector.prefilter import Prefilter

BOARD = {"name": "Microsoft", "endpoint": "https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"}


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(eightfold, "PACE_S", 0)


def pos(i, locs=("India, Karnataka, Bangalore",)):
    return {"id": 1970393557000000 + i, "displayJobId": str(200000000 + i), "name": " Applied Scientist II ",
            "locations": list(locs), "postedTs": 1795274517, "positionUrl": f"/careers/job/{1970393557000000 + i}"}


def client(total, first_429=True, detail=None):
    state = {"challenged": not first_429}
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if not state["challenged"]:  # first call: cookie challenge
            state["challenged"] = True
            return httpx.Response(429, text="Please try again later", headers={"set-cookie": "_vs=1; Path=/"})
        if req.url.path == "/api/pcsx/search":
            assert req.url.params["location"] == "India" and req.url.params["sort_by"] == "timestamp"
            s = int(req.url.params["start"])
            return httpx.Response(200, json={"data": {"count": total, "positions": [pos(i) for i in range(s, min(s + 10, total))]}})
        if req.url.path == "/api/pcsx/position_details":
            return httpx.Response(200, json={"data": detail}) if detail else httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, text="<html></html>")
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_retries_cookie_challenge_and_pages():
    c, calls = client(25)
    jobs = eightfold.fetch(c, BOARD)
    assert len(jobs) == 25 and len(calls) == 4  # 429 + 3 pages
    j = jobs[0]
    assert j.title == "Applied Scientist II" and j.req_id == "200000000"
    assert j.posted_at == "2026-11-21T15:21:57+00:00"
    assert j.url == "https://apply.careers.microsoft.com/careers/job/1970393557000000"
    assert j.apply_url == "https://apply.careers.microsoft.com/careers/apply?pid=1970393557000000&domain=microsoft.com"
    assert Prefilter.load().location_ok(j.locations[0])


def test_midnight_utc_posted_ts_is_a_date():
    # Qualcomm (careers.qualcomm.com, 2026-11-27): every postedTs is 00:00 UTC, i.e. a date only.
    assert eightfold.posted(1795737600) == "2026-11-27"
    assert eightfold.posted(1795274517) == "2026-11-21T15:21:57+00:00"
    assert eightfold.posted(None) is None


def test_incremental_stops_on_page_with_nothing_new():
    c, calls = client(230, first_429=False)
    eightfold.fetch(c, BOARD, skip={str(1970393557000000 + i) for i in range(3, 230)}, full=False)
    assert len(calls) == 2


def test_persistent_429_is_an_error():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="Please try again later")))
    with pytest.raises(eightfold.EightfoldError):
        eightfold.fetch(c, BOARD)


def test_enrich_reads_all_locations_and_handoff_apply():
    c, _ = client(1, first_429=False, detail={"locations": ["India, Haryana, Gurgaon", "India, Telangana, Hyderabad"],
        "positionUserActions": {"applyAction": {"status": "link_off", "applyUrl": "https://x.wd5.myworkdayjobs.com/E/job/1/apply"}}})
    [j] = eightfold.fetch(c, BOARD)
    eightfold.enrich(c, j)
    assert j.locations == ["India, Haryana, Gurgaon", "India, Telangana, Hyderabad"]
    assert j.apply_url.endswith("/apply") and "myworkdayjobs" in j.apply_url
    assert eightfold.live(c, j)[0]


def test_closed_position_detail_404_raises():
    c, _ = client(1, first_429=False, detail=None)
    [j] = eightfold.fetch(c, BOARD)
    with pytest.raises(eightfold.EightfoldError):
        eightfold.enrich(c, j)


# --- v2 API (HSBC, recorded 2026-11-25) ---
V2 = {"name": "HSBC", "endpoint": "https://portal.careers.hsbc.com/api/apply/v2/jobs?domain=hsbc.com"}


def v2_row(i):
    pid = 563774612545977 + i
    return {"id": pid, "name": "Data Scientist", "posting_name": " Data Scientist / Assistant Vice President ",
            "location": "Pune, Maharashtra, India", "locations": ["Pune, Maharashtra, India"],
            "t_create": 1795525596, "ats_job_id": str(64903 + i), "display_job_id": str(64903 + i),
            "canonicalPositionUrl": f"https://portal.careers.hsbc.com/careers/job/{pid}",
            "custom_data": {"postedDate": None}}


def v2_client(total, detail=None, sf_title="Career Opportunities: Data Scientist (64903)"):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/api/apply/v2/jobs":
            assert req.url.params["location"] == "India" and req.url.params["sort_by"] == "timestamp"
            s = int(req.url.params["start"])
            return httpx.Response(200, json={"count": total, "positions": [v2_row(i) for i in range(s, min(s + 10, total))]})
        if req.url.path.startswith("/api/apply/v2/jobs/"):
            return httpx.Response(200, json=detail) if detail else httpx.Response(404, text="not found")
        if "successfactors" in req.url.host:
            return httpx.Response(200, text=f"<html><title>{sf_title}</title></html>")
        return httpx.Response(200, text="<html></html>")
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_v2_fetch_parses_rows_without_a_stated_date():
    c, calls = v2_client(15)
    jobs = eightfold.fetch(c, V2)
    assert len(jobs) == 15 and len(calls) == 2
    j = jobs[0]
    assert j.title == "Data Scientist / Assistant Vice President" and j.req_id == "64903"
    assert j.posted_at is None  # t_create is Eightfold's record time, not a stated date
    assert j.url == "https://portal.careers.hsbc.com/careers/job/563774612545977"
    assert j.check_urls == ["https://portal.careers.hsbc.com/api/apply/v2/jobs/563774612545977?domain=hsbc.com"]


def test_v2_incremental_reads_past_pinned_first_page():
    c, calls = v2_client(205)
    eightfold.fetch(c, V2, skip={str(563774612545977 + i) for i in range(205)}, full=False)
    assert len(calls) == 2


SF = "https://career2.successfactors.eu/careers?company=hsbcholdin&career_ns=job_application&career_job_req_id=64903"


def test_v2_enrich_and_successfactors_liveness():
    c, _ = v2_client(1, detail={"locations": ["Pune, Maharashtra, India"], "job_description": "<p>Python</p>",
                                "apply_redirect_url": SF})
    [j] = eightfold.fetch(c, V2)
    eightfold.enrich(c, j)
    assert j.apply_url == SF and "Python" in j.jd
    assert eightfold.live(c, j)[0]


def test_v2_successfactors_signin_page_is_not_live():
    c, _ = v2_client(1, detail={"apply_redirect_url": SF}, sf_title="Career Opportunities: Sign In")
    [j] = eightfold.fetch(c, V2)
    eightfold.enrich(c, j)
    assert not eightfold.live(c, j)[0]


def test_v2_closed_detail_404_raises():
    c, _ = v2_client(1)
    [j] = eightfold.fetch(c, V2)
    with pytest.raises(eightfold.EightfoldError):
        eightfold.enrich(c, j)


# --- v2 API applying on Eightfold itself (Millennium, recorded 2026-11-26) ---
MLP = {"name": "Millennium", "endpoint": "https://mlp.eightfold.ai/api/apply/v2/jobs?domain=mlp.com"}


def mlp_client(job_page=200):
    def handler(req: httpx.Request):
        if req.url.path == "/api/apply/v2/jobs":
            return httpx.Response(200, json={"count": 1, "positions": [{
                "id": 755959842482, "name": "Applied AI Engineer - Operations & Middle Office",
                "location": "Bangalore, Karnataka, India", "locations": ["Bangalore, Karnataka, India"],
                "t_create": 1794960000, "ats_job_id": "REQ-32011", "display_job_id": "REQ-32011",
                "canonicalPositionUrl": "https://mlp.eightfold.ai/careers/job/755959842482", "custom_data": None}]})
        if req.url.path == "/api/apply/v2/jobs/755959842482":
            return httpx.Response(200, json={"locations": ["Bangalore, Karnataka, India"],
                                             "job_description": "<p>LLM agents in Python</p>", "apply_redirect_url": None})
        if req.url.path == "/careers/job/755959842482":
            return httpx.Response(job_page, text="<html></html>")
        if req.url.path == "/careers/apply":  # a JS shell: 200 for any pid
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_v2_eightfold_apply_uses_detail_api_and_job_page():
    c = mlp_client()
    [j] = eightfold.fetch(c, MLP)
    assert j.req_id == "REQ-32011" and j.posted_at is None  # custom_data is null
    eightfold.enrich(c, j)
    assert "LLM agents" in j.jd
    assert j.apply_url == "https://mlp.eightfold.ai/careers/apply?pid=755959842482&domain=mlp.com"  # no hand-off
    assert eightfold.live(c, j)[0]


def test_v2_eightfold_apply_closed_job_page_is_not_live():
    c = mlp_client(job_page=404)
    [j] = eightfold.fetch(c, MLP)
    eightfold.enrich(c, j)
    ok, why = eightfold.live(c, j)
    assert not ok and "JD HTTP 404" in why
