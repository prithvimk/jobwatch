"""Workday adapter against recorded response shapes (curl is replaced by a fake)."""
import json

import pytest

from detector import workday
from detector.prefilter import Prefilter

PF = Prefilter.load()
EP = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"


def posting(n, loc="Pune, Maharashtra, India", title="ML Engineer"):
    return {"title": title, "externalPath": f"/job/Pune/{title.replace(' ', '-')}_R{n}", "locationsText": loc,
            "postedOn": "Posted Today", "bulletFields": [f"R{n}"]}


class FakeCurl:
    def __init__(self, total, facets=None, detail=None):
        self.total, self.facets, self.detail, self.calls = total, facets or [], detail or {}, []

    def __call__(self, url, body=None):
        self.calls.append((url, body))
        if body is None:  # detail API or a page GET
            if "/wday/cxs/" in url:
                d = self.detail.get(url.rsplit("_", 1)[-1])
                return (200, url, json.dumps({"jobPostingInfo": d})) if d else (404, url, "{}")
            return 200, url, "<html></html>"
        if body["limit"] == 1:
            return 200, url, json.dumps({"total": self.total, "facets": self.facets, "jobPostings": []})
        off = body["offset"]
        page = [posting(i) for i in range(off, min(off + body["limit"], self.total))]
        return 200, url, json.dumps({"total": self.total if off == 0 else 0, "facets": self.facets, "jobPostings": page})


@pytest.fixture
def fake(monkeypatch):
    def make(**kw):
        f = FakeCurl(**kw)
        monkeypatch.setattr(workday, "curl", f)
        return f
    return make


def test_urls_both_host_styles():
    assert workday.urls(EP) == ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers",
                                "https://acme.wd5.myworkdayjobs.com/Careers")
    assert workday.urls("https://wd1.myworkdaysite.com/wday/cxs/wf/WellsFargoJobs/jobs")[1] == \
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs"


def test_job_id_is_requisition_suffix():
    assert workday.job_id("/job/Pune-Maharashtra-India/Upskilling--AVP_261008205-1") == "261008205-1"


def test_full_sweep_pages_everything_with_country_facet(fake):
    f = fake(total=45)
    b = {"name": "Acme", "endpoint": EP, "india_facet": {"param": "locationCountry", "id": "IN"}}
    jobs = workday.fetch(b, skip=set(), location_ok=PF.location_ok, full=True)
    assert len(jobs) == 45 and len(f.calls) == 3
    assert f.calls[0][1]["appliedFacets"] == {"locationCountry": ["IN"]}
    j = jobs[0]
    assert j.id == "R0" and j.req_id == "R0" and j.posted_at is None
    assert j.url == "https://acme.wd5.myworkdayjobs.com/Careers/job/Pune/ML-Engineer_R0"
    assert j.check_urls == ["https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/Pune/ML-Engineer_R0"]


def test_incremental_stops_at_first_page_with_nothing_unseen(fake):
    f = fake(total=100)
    b = {"name": "Acme", "endpoint": EP, "india_facet": {"param": "c", "id": "IN"}}
    seen = {f"R{i}" for i in range(3, 100)}  # R0-R2 are new and on page 1; page 2 is all known
    jobs = workday.fetch(b, skip=seen, location_ok=PF.location_ok, full=False)
    assert len(f.calls) == 2 and len(jobs) == 40


def test_city_level_tenant_uses_allowed_city_facets(fake):
    facets = [{"facetParameter": "locations", "values": [
        {"descriptor": "Bangalore, India", "id": "blr"}, {"descriptor": "Gurgaon, India", "id": "ggn"},
        {"descriptor": "Pune, Gera Commerzone SEZ", "id": "pune"}, {"descriptor": "Indianapolis, Indiana, US", "id": "x"}]},
        {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Mumbai Ops", "id": "nope"}]}]
    f = fake(total=5, facets=facets)
    workday.fetch({"name": "Acme", "endpoint": EP}, skip=set(), location_ok=PF.location_ok, full=True)
    assert f.calls[1][1]["appliedFacets"] == {"locations": ["blr", "pune"]}


def test_city_level_tenant_without_india_is_an_error(fake):
    fake(total=5, facets=[{"facetParameter": "locations", "values": [{"descriptor": "Tokyo", "id": "t"}]}])
    with pytest.raises(workday.WorkdayError):
        workday.fetch({"name": "Acme", "endpoint": EP}, skip=set(), location_ok=PF.location_ok, full=True)


def test_multi_location_filled_by_detail(fake):
    fake(total=1, detail={"R0": {"startDate": "2026-11-20", "location": "Gurugram, India",
                                 "additionalLocations": ["Pune Maharashtra India"], "canApply": True,
                                 "externalUrl": "https://acme.wd5.myworkdayjobs.com/Careers/job/Pune/ML-Engineer_R0"}})
    [j] = workday.fetch({"name": "Acme", "endpoint": EP, "india_facet": {"param": "c", "id": "IN"}},
                        skip=set(), location_ok=PF.location_ok, full=True)
    j.locations = []  # as when the list says "2 Locations"
    assert PF.check(j)[0]  # kept until the detail says where
    workday.enrich(None, j)
    assert j.posted_at == "2026-11-20" and j.locations == ["Gurugram, India", "Pune Maharashtra India"]
    assert PF.check(j)[0] and workday.live(None, j)[0]
    assert j.apply_url.endswith("ML-Engineer_R0/apply")


def test_closed_posting_detail_404_raises(fake):
    fake(total=1)
    [j] = workday.fetch({"name": "Acme", "endpoint": EP, "india_facet": {"param": "c", "id": "IN"}},
                        skip=set(), location_ok=PF.location_ok, full=True)
    with pytest.raises(workday.WorkdayError):
        workday.enrich(None, j)


def test_row_without_link_is_skipped_and_logged(fake, monkeypatch, capsys):
    f = fake(total=25)
    orig = f.__call__
    def stub_row(url, body=None):
        code, u, text = orig(url, body)
        d = json.loads(text)
        if body and body["offset"] == 0:
            d["jobPostings"][5] = {"bulletFields": ["R0124147"]}  # as LSEG returned, 2026-11-23
        return code, u, json.dumps(d)
    monkeypatch.setattr(workday, "curl", stub_row)
    jobs = workday.fetch({"name": "Acme", "endpoint": EP, "india_facet": {"param": "c", "id": "IN"}},
                         skip=set(), location_ok=PF.location_ok, full=True)
    assert len(jobs) == 24 and "R5" not in {j.id for j in jobs} and "R24" in {j.id for j in jobs}
    assert "no link, unknown (R0124147)" in capsys.readouterr().out


def test_list_multi_location_text_left_empty(fake):
    fake(total=0)
    f = workday.curl
    f.total = 1
    orig = f.__call__
    def two_locs(url, body=None):
        code, u, text = orig(url, body)
        d = json.loads(text)
        for p in d.get("jobPostings") or []:
            p["locationsText"] = "2 Locations"
        return code, u, json.dumps(d)
    workday.curl = two_locs
    [j] = workday.fetch({"name": "Acme", "endpoint": EP, "india_facet": {"param": "c", "id": "IN"}},
                        skip=set(), location_ok=PF.location_ok, full=True)
    assert j.locations == []
