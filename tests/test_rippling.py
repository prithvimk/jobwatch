"""Rippling ATS adapter against recorded response shapes (Rippling, 2026-11-26)."""
import json

import httpx
import pytest

from detector import rippling
from detector.prefilter import Prefilter

BOARD = {"name": "Rippling", "endpoint": "https://ats.rippling.com/api/v2/board/rippling/jobs"}
OPEN = "336db642-a679-41de-b195-b3a1ebb60441"
CLOSED = "84d388b6-7656-434c-8862-0312eb6b97ac"


def item(uid, name, *locs):
    return {"id": uid, "name": name, "url": f"https://ats.rippling.com/rippling/jobs/{uid}",
            "department": {"name": "Engineering"}, "language": "en-US",
            "locations": [{"name": n, "country": c, "countryCode": cc, "city": n.split(",")[0],
                           "workplaceType": "ON_SITE"} for n, c, cc in locs]}


BLR = ("Bangalore, India", "India", "IN")
SF = ("San Francisco, CA", "United States", "US")
ITEMS = [item(OPEN, " Senior Software Engineer - AI Initiative", BLR),
         item(CLOSED, "Business Operations Manager", SF),
         item("aaaa", "ML Engineer", SF, BLR)]


def page(job_post):
    data = {"props": {"pageProps": {"apiData": {"jobPost": job_post}}}}
    return httpx.Response(200, html=f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>')


def client(items=ITEMS, total=None, open_ids=(OPEN,)):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.host == "ats.rippling.com" and req.url.path == "/api/v2/board/rippling/jobs":
            assert req.url.params["pageSize"] == "1000"
            return httpx.Response(200, json={"page": 0, "pageSize": 1000, "totalItems": total or len(items),
                                             "totalPages": 1, "items": items})
        if req.url.host == "ats.rippling.com" and req.url.path.startswith("/rippling/jobs/"):
            uid = req.url.path.split("/")[3]
            if uid not in open_ids:  # closed or unknown: redirect to the careers landing page
                return httpx.Response(302, headers={"location": "https://www.rippling.com/careers/open-roles"})
            if req.url.path.endswith("/apply"):
                return httpx.Response(200, html="<html>apply</html>")
            return page({"uuid": uid, "name": "Senior Software Engineer - AI Initiative",
                         "workLocations": ["Bangalore, India"], "createdOn": "2026-08-10T01:11:07-07:00",
                         "description": {"company": "<p>About Rippling</p>", "role": "<p>3+ years with LLMs</p>"}})
        if req.url.host == "www.rippling.com":
            return httpx.Response(200, html="<html>Open roles</html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), calls


def test_fetch_keeps_india_postings_in_one_call():
    c, calls = client()
    jobs = rippling.fetch(c, BOARD)
    assert len(calls) == 1 and [j.id for j in jobs] == [OPEN, "aaaa"]
    j = jobs[0]
    assert j.title == "Senior Software Engineer - AI Initiative" and j.locations == ["Bangalore, India"]
    assert j.url == f"https://ats.rippling.com/rippling/jobs/{OPEN}" and j.apply_url == j.url + "/apply"
    assert j.posted_at is None  # no stated posting date
    assert jobs[1].locations == ["Bangalore, India"]  # only the Indian location is kept
    assert Prefilter.load().location_ok(j.locations[0])


def test_fetch_refuses_a_truncated_list():
    c, _ = client(total=1500)
    with pytest.raises(rippling.RipplingError, match="1500"):
        rippling.fetch(c, BOARD)


def test_enrich_reads_jd_and_live():
    c, _ = client()
    j = rippling.fetch(c, BOARD)[0]
    rippling.enrich(c, j)
    assert j.jd.startswith("3+ years with LLMs") and "About Rippling" in j.jd
    assert j.posted_at is None  # createdOn is a record time, not used
    assert rippling.live(c, j)[0]


def test_closed_posting_redirects_away():
    c, _ = client(open_ids=())
    j = rippling.fetch(c, BOARD)[0]
    with pytest.raises(rippling.RipplingError, match="open-roles"):
        rippling.enrich(c, j)
    ok, why = rippling.live(c, j)
    assert not ok and "open-roles" in why
