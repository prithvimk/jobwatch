"""Oracle Recruiting Cloud adapter against recorded response shapes (JPMC, 2026-11-21)."""
import httpx
import pytest

from detector import oracle
from detector.prefilter import Prefilter
from detector.run import format_alert

BOARD = {"name": "JPMC", "locator": ["jpmc.fa.oraclecloud.com", "CX_1001"]}


def row(i, loc="Bengaluru, Karnataka, India", sec=(), date="2026-11-20"):
    return {"Id": str(210800000 + i), "Title": "Applied AI ML Associate", "PostedDate": date, "PrimaryLocation": loc,
            "secondaryLocations": [{"Name": s, "CountryCode": "IN"} for s in sec]}


def client(rows, detail=None):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        finder = req.url.params.get("finder", "")
        if "recruitingCEJobRequisitionDetails" in req.url.path:
            return httpx.Response(200, json={"items": [detail] if detail else [], "count": int(bool(detail))})
        if "facetsList=LOCATIONS" in finder:
            return httpx.Response(200, json={"items": [{"TotalJobsCount": 9000, "requisitionList": [],
                "locationsFacet": [{"Id": 1, "Name": "United States", "TotalCount": 5000},
                                   {"Id": 300000000289360, "Name": "India", "TotalCount": len(rows)}]}]})
        assert "locationId=300000000289360" in finder and "sortBy=POSTING_DATES_DESC" in finder
        off = int(finder.split("offset=")[1].split(",")[0])
        return httpx.Response(200, json={"items": [{"TotalJobsCount": len(rows), "requisitionList": rows[off:off + oracle.PAGE]}]})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_pages_india_and_reads_secondary_locations():
    rows = [row(i) for i in range(250)]
    rows[0] = row(0, loc="Gurugram, Haryana, India", sec=["Mumbai, Maharashtra, India"])
    c, calls = client(rows)
    jobs = oracle.fetch(c, BOARD)
    assert len(jobs) == 250 and len(calls) == 3  # facet lookup + 2 pages of 200
    j = jobs[0]
    assert j.locations == ["Gurugram, Haryana, India", "Mumbai, Maharashtra, India"] and j.posted_at == "2026-11-20"
    assert j.url == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210800000"
    assert Prefilter.load().check(j)[0]  # kept via Mumbai


def test_missing_india_facet_is_an_error():
    c, _ = client([])
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"items": [{"locationsFacet": []}]}))
    with pytest.raises(oracle.OracleError):
        oracle.fetch(c, BOARD)


def test_enrich_sets_exact_post_time_and_closing_date():
    c, _ = client([row(1)], detail={"ExternalPostedStartDate": "2026-11-21T07:01:17+00:00",
                                    "ExternalPostedEndDate": "2026-11-25T05:00:00+00:00"})
    [j] = oracle.fetch(c, BOARD)
    oracle.enrich(c, j)
    assert j.posted_at == "2026-11-21T07:01:17+00:00" and j.closes_at == "2026-11-25T05:00:00+00:00"
    _, body = format_alert(j, "2026-11-21T08:00:00+00:00", "ok")
    assert "Posted 21 Nov 12:31 IST (per ATS). Closes 25 Nov 10:30 IST" in body


def test_closed_posting_has_no_detail():
    c, _ = client([row(1)], detail=None)
    [j] = oracle.fetch(c, BOARD)
    with pytest.raises(oracle.OracleError):
        oracle.enrich(c, j)
