"""BrassRing adapter against recorded response shapes (UBS, jobs.ubs.com, 2026-11-26)."""
import html
import json

import httpx
import pytest

from detector import brassring
from detector.prefilter import Prefilter

BOARD = {"name": "UBS", "endpoint": "https://jobs.ubs.com/TgNewUI/Search/Ajax/MatchedJobs?partnerid=25008&siteid=5012"}
PF = Prefilter.load()


def row(rid, title, city, country="India"):
    qs = {"reqid": rid, "clientid": "25008", "siteid": "5012", "lastupdated": "26-Nov-2026", "jobtitle": title,
          "formtext2": city, "formtext23": country, "department": "Group Functions "}
    return {"Questions": [{"QuestionName": k, "Value": v} for k, v in qs.items()],
            "Link": f"https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5012&PageType=JobDetails&jobid={rid}"}


CITY_FACET = {"Name": "formtext2", "Description": "City", "Options": [
    {"OptionName": "New York", "OptionValue": "New York", "Count": 82},
    {"OptionName": "Pune", "OptionValue": "Pune", "Count": 27},
    {"OptionName": "Mumbai", "OptionValue": "Mumbai", "Count": 7},
    {"OptionName": "Indianapolis", "OptionValue": "Indianapolis", "Count": 1}]}
INDIA = [row(str(352000 + i), "Data Scientist - GenAI &amp; NLP" if i == 0 else f"Role {i}", "Pune" if i % 2 else "Mumbai")
         for i in range(55)]
DETAIL = {"Jobdetails": {"isActive": "True", "AutoOptionalReqId": "344740BR", "Title": "Data Scientist - GenAI & NLP",
                         "JobDetailQuestions": [
                             {"QuestionName": "", "AnswerValue": "India"},
                             {"QuestionName": "City", "AnswerValue": "Pune"},
                             {"QuestionName": "Your skills and experience",
                              "AnswerValue": "You bring:<br>• 3+ years building LLM and RAG systems in Python and PyTorch"},
                             {"QuestionName": "About us", "AnswerValue": "UBS is a leading and truly global wealth manager " * 3}]}}


def page_html(payload):
    value = html.escape(json.dumps(payload), quote=True)
    return f'<html><input id="preLoadJSON" type="hidden" value="{value}" /></html>'


def client(open_ids=("352000",)):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        p = req.url.path
        if p == "/TGnewUI/Search/Home/Home":
            return httpx.Response(200, html='<input name="__RequestVerificationToken" type="hidden" value="tok123" />')
        if p.startswith("/TgNewUI/Search/Ajax/"):
            assert req.headers["RFT"] == "tok123"
            body = json.loads(req.content)
            assert body["partnerId"] == "25008" and body["siteId"] == "5012"
            if p.endswith("/MatchedJobs"):
                return httpx.Response(200, json={"Jobs": {"Job": [row("1", "Banker", "Zürich", "Switzerland")]},
                                                 "JobsCount": 602, "Facets": {"Facet": [CITY_FACET]}})
            sel = body["facetfilterfields"]["Facet"][0]
            assert sel["Name"] == "formtext2" and [o["OptionValue"] for o in sel["Options"]] == ["Pune", "Mumbai"]
            n = body["pageNumber"]
            return httpx.Response(200, json={"Jobs": {"Job": INDIA[(n - 1) * 50:n * 50]}, "JobsCount": len(INDIA)})
        if p == "/TGnewUI/Search/home/HomeWithPreLoad":
            rid = req.url.params["jobid"]
            return httpx.Response(200, html=page_html(DETAIL if rid in open_ids else {"Jobdetails": None}))
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_filters_to_allowed_indian_cities_and_pages():
    c, calls = client()
    jobs = brassring.fetch(c, BOARD, location_ok=PF.location_ok)
    assert len(jobs) == 55
    assert [r.url.path.rsplit("/", 1)[-1] for r in calls] == ["Home", "MatchedJobs"] + ["ProcessSortAndShowMoreJobs"] * 2
    j = jobs[0]
    assert j.id == "352000" and j.title == "Data Scientist - GenAI & NLP" and j.locations == ["Mumbai, India"]
    assert j.url.endswith("&jobid=352000") and j.apply_url == j.url and j.posted_at is None


def test_no_allowed_city_returns_nothing():
    c, calls = client()
    assert brassring.fetch(c, BOARD, location_ok=lambda l: False) == [] and len(calls) == 2


def test_enrich_reads_reference_and_jd_and_skips_boilerplate():
    c, _ = client()
    j = brassring.fetch(c, BOARD, location_ok=PF.location_ok)[0]
    brassring.enrich(c, j)
    assert j.req_id == "344740BR" and j.open
    assert "LLM and RAG" in j.jd and "Your skills and experience" in j.jd and "wealth manager" not in j.jd
    assert j.locations == ["Pune", "Mumbai, India"]
    assert brassring.live(c, j)[0]


def test_closed_job_raises():
    c, _ = client(open_ids=())
    j = brassring.fetch(c, BOARD, location_ok=PF.location_ok)[0]
    with pytest.raises(brassring.BrassRingError, match="closed or unknown"):
        brassring.enrich(c, j)
