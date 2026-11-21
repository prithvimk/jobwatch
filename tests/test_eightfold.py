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
