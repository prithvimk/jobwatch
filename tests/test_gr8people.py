"""gr8 People adapter against recorded response shapes (Teradata, 2026-11-22)."""
import json

import httpx
import pytest

from detector import gr8people
from detector.prefilter import Prefilter

BOARD = {"name": "Teradata", "endpoint": "https://careers.teradata.com/graphql"}


def node(i, places=("Hyderabad, Telangana, India",), status="OPEN"):
    return {"__typename": "JobPosting", "key": str(220000 + i), "number": str(220000 + i),
            "title": "Senior AI Engineer — Agents", "status": status, "postedOn": "2026-11-20T21:44:34.68Z",
            "places": {"nodes": [{"name": p} for p in places]}}


def page(job, status=200):
    data = {"props": {"pageProps": {"job": job}}}
    return httpx.Response(status, html=f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>')


def client(nodes, job_page=None, need_cookie=True):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/jobs":
            return httpx.Response(200, html="<html></html>", headers={"set-cookie": "token=t; Path=/"})
        if req.url.path == "/graphql":
            if need_cookie and "token=t" not in req.headers.get("cookie", ""):
                return httpx.Response(200, json={"errors": [{"extensions": {"code": "UNAUTHENTICATED"}}], "data": {"searchJobs": None}})
            v = json.loads(req.content)["variables"]
            s, n = v["start"], v["first"]
            return httpx.Response(200, json={"data": {"searchJobs": {"results": {
                "totalCount": len(nodes), "nodes": nodes[s:s + n]}}}})
        if req.url.path.endswith("/apply"):
            return httpx.Response(200, html="<html>sign in</html>")
        if req.url.path.startswith("/jobs/"):
            return job_page or httpx.Response(404, html="<html>Not found</html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_pages_all_and_keeps_india():
    nodes = [node(i) for i in range(150)] + [node(200, places=("San Diego, CA, USA",)),
                                             node(201, places=("San Diego, CA, USA", "Pune, Maharashtra, India"))]
    c, calls = client(nodes)
    jobs = gr8people.fetch(c, BOARD)
    assert len(jobs) == 151 and len([r for r in calls if r.url.path == "/graphql"]) == 2
    j = jobs[0]
    assert j.id == "220000" and j.req_id == "220000" and j.title == "Senior AI Engineer — Agents"
    assert j.url == "https://careers.teradata.com/jobs/220000/senior-ai-engineer-agents"
    assert j.apply_url == j.url + "/apply"
    assert j.posted_at == "2026-11-20T21:44:34.68Z"
    assert Prefilter.load().location_ok(j.locations[0])
    assert jobs[-1].locations == ["San Diego, CA, USA", "Pune, Maharashtra, India"]


def test_graphql_error_is_raised():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "errors": [{"extensions": {"code": "UNAUTHENTICATED"}}], "data": {"searchJobs": None}})))
    with pytest.raises(gr8people.Gr8Error, match="UNAUTHENTICATED"):
        gr8people.fetch(c, BOARD)


def test_enrich_reads_embedded_posting_and_live():
    job = dict(node(1, places=("Bengaluru, Karnataka, India", "Gurugram, Haryana, India")), descriptionHTML="<p>3+ years of ML</p>")
    c, _ = client([node(1)], job_page=page(job))
    [j] = gr8people.fetch(c, BOARD)
    gr8people.enrich(c, j)
    assert j.locations == ["Bengaluru, Karnataka, India", "Gurugram, Haryana, India"]
    assert j.jd == "3+ years of ML"
    assert gr8people.live(c, j)[0]


def test_closed_posting_raises():
    c, _ = client([node(1)], job_page=None)  # job page 404
    [j] = gr8people.fetch(c, BOARD)
    with pytest.raises(gr8people.Gr8Error, match="404"):
        gr8people.enrich(c, j)
    c, _ = client([node(1)], job_page=page(node(1, status="CLOSED")))
    with pytest.raises(gr8people.Gr8Error, match="CLOSED"):
        gr8people.enrich(c, j)
