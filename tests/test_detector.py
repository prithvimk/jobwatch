import json

import httpx
import pytest

from detector import run as runmod
from detector import sources, state
from detector.prefilter import Prefilter
from detector.sources import Job

PF = Prefilter.load()


def job(title="ML Engineer", locs=("Bengaluru, India",), board="Databricks"):
    return Job(board=board, id="1", title=title, locations=list(locs), url="u", apply_url="a", posted_at=None)


# ---------------------------------------------------------------- prefilter

@pytest.mark.parametrize("loc,ok", [
    ("Bengaluru, India", True), ("Mumbai, MH, India", True), ("India", True), ("Remote - India", True),
    ("Gurugram, Haryana, India", False), ("Noida", False), ("New Delhi, India", False), ("Jaipur, India", False),
    ("Chandigarh", False), ("Bangalore / Gurgaon", True), ("Hyderabad; Noida", True),
    ("IND-BLR-Divyasree Technopolis", True), ("IND-DEL-Okhla", False), ("IND-GGN-DLF", False), ("Indianapolis, IN-US", False),
    ("Tokyo, Japan", False), ("San Francisco, CA", False),
    ("ITGOA01 - Genoa - Magazzini del Cotone", False), ("Panaji, Goa, India", True),  # Maersk, 2026-11-25
])
def test_location(loc, ok):
    assert PF.location_ok(loc) is ok


def test_multi_location_any_allowed():
    assert PF.check(job(locs=["Gurugram, India", "Pune, India"]))[0]
    ok, why = PF.check(job(locs=["Gurugram, India", "Noida, India"]))
    assert not ok and "North India" in why


@pytest.mark.parametrize("title,ok", [
    ("Machine Learning Engineer", True), ("Senior GenAI Engineer", True), ("Applied Scientist II", True),
    ("Data Engineer", True),  # spec exception: screener decides from required quals
    ("Applied AIML Associate Senior, AWM Predictive Science", True), ("Applied AI/ML Associate", True),
    ("Predictive Modeling Analyst", True), ("Associate, Predictive Science", True),
    ("Artificial Intelligence Engineer (III)", True), ("Agent Engineer", True), ("Forward Deployed Engineer", True),
    ("Forward Deploy Engineer, Aladdin Data, Associate", True), ("Member of Technical Staff, Inference", True),
    ("Quant Developer - Front Office", True), ("Analyst /Sr Analytics & Modeling Analyst", True),
    ("Associate - Strategic Data Analytics", True), ("Sr Full Stack Developer (AI Agents)", True),
    ("Sr. SW Engineer (3-5 yrs, GenAI, Java/Python full stack)", True), ("Senior Full Stack Engineer", False),
    ("Software Engineer, Backend", False), ("Principal ML Engineer", False), ("Staff AI Engineer", False),
    ("AI Product Manager", False), ("ML Intern", False), ("Android Engineer - AI", False), ("AI Sales Lead", False),
])
def test_title(title, ok):
    assert PF.check(job(title=title))[0] is ok


def test_board_extra_include():
    assert PF.check(job(title="SDE2 - API Platform", board="Sarvam"))[0]
    assert not PF.check(job(title="SDE2 - API Platform", board="Databricks"))[0]
    assert not PF.check(job(title="Motion Designer", board="Sarvam"))[0]


# ---------------------------------------------------------------- source parsers (fixtures)

def mock_client(routes: dict) -> httpx.Client:
    def handler(req: httpx.Request):
        for frag, body in routes.items():
            if frag in str(req.url):
                return httpx.Response(200, json=body)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse_parse():
    c = mock_client({"boards/acme/jobs": {"jobs": [{"id": 42, "title": " ML Engineer ", "location": {"name": "Pune, India"},
        "absolute_url": "https://acme.com/j?gh_jid=42", "first_published": "2026-11-20T10:00:00-04:00", "requisition_id": "R-7"}]}})
    [j] = sources.greenhouse(c, "Acme", "acme")
    assert (j.id, j.title, j.locations, j.posted_at, j.req_id) == ("42", "ML Engineer", ["Pune, India"], "2026-11-20T10:00:00-04:00", "R-7")
    assert j.apply_url == "https://job-boards.greenhouse.io/acme/jobs/42"


def test_greenhouse_reads_location_metadata():
    # Cloudflare: `location` is only the work style; cities are in a "Job Posting Location" field
    c = mock_client({"boards/cf/jobs": {"jobs": [{"id": 7, "title": "ML Engineer", "location": {"name": "Hybrid"},
        "metadata": [{"name": "Cost Center", "value": "5150"},
                     {"name": "Job Posting Location", "value": ["Bengaluru, India", "Lisbon, Portugal"]}]}]}})
    [j] = sources.greenhouse(c, "Cloudflare", "cf")
    assert j.locations == ["Hybrid", "Bengaluru, India", "Lisbon, Portugal"]


def test_ashby_parse_skips_unlisted_and_adds_country():
    c = mock_client({"job-board/acme": {"jobs": [
        {"id": "a", "title": "LLM Engineer", "location": "Bengaluru", "secondaryLocations": [{"location": "Pune"}],
         "address": {"postalAddress": {"addressCountry": "India"}}, "jobUrl": "J", "applyUrl": "A", "publishedAt": "2026-11-20T00:00:00Z"},
        {"id": "b", "title": "Hidden", "isListed": False, "location": "X", "jobUrl": "J"}]}})
    [j] = sources.ashby(c, "Acme", "acme")
    assert j.locations == ["Bengaluru, India", "Pune"] and j.apply_url == "A"


def test_smartrecruiters_paginates():
    pages = iter([{"totalFound": 2, "content": [{"id": 1, "name": "AI Eng", "location": {"fullLocation": "Mumbai, MH, India"}, "releasedDate": "d"}]},
                  {"totalFound": 2, "content": [{"id": 2, "name": "ML Eng", "location": {"city": "Pune", "country": "in"}}]}])
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(pages))))
    jobs = sources.smartrecruiters(c, "Acme", "Acme")
    assert [j.id for j in jobs] == ["1", "2"] and jobs[1].locations == ["Pune, in"]


# ---------------------------------------------------------------- run: baseline, diff, liveness, state

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runmod, "load_boards", lambda names: [{"name": "Acme", "ats": "fake", "locator": ["acme"]}])
    pushed = []
    monkeypatch.setattr(runmod.notify, "push", lambda title, *a, **k: pushed.append(title) or True)
    live = {"ok": True}
    monkeypatch.setattr(runmod, "liveness", lambda c, j: (live["ok"], "test"))
    postings = []
    monkeypatch.setitem(runmod.FETCHERS, "fake", lambda c, b, **k: list(postings))
    return tmp_path, postings, pushed, live


def seen(tmp):
    return json.loads((tmp / "seen.json").read_text())["boards"]["Acme"]["jobs"]


def test_baseline_then_diff(env):
    tmp, postings, pushed, _ = env
    postings.append(Job("Acme", "1", "ML Engineer", ["Pune, India"], "u", "a", None))
    runmod.run(None, dry_run=False)
    assert pushed == [] and seen(tmp)["1"]["status"] == "baseline"

    postings.append(Job("Acme", "2", "GenAI Engineer", ["Mumbai, India"], "u", "a", None))
    postings.append(Job("Acme", "3", "Recruiter", ["Mumbai, India"], "u", "a", None))
    runmod.run(None, dry_run=False)
    assert pushed == ["Acme: GenAI Engineer"]
    s = seen(tmp)
    assert s["2"]["status"] == "alerted" and s["3"]["status"] == "filtered"

    runmod.run(None, dry_run=False)  # nothing new: no repeat alert
    assert pushed == ["Acme: GenAI Engineer"]


def test_removed_then_reposted_same_id_not_new(env):
    tmp, postings, pushed, _ = env
    runmod.run(None, dry_run=False)  # baseline (empty)
    postings.append(Job("Acme", "9", "ML Engineer", ["Pune, India"], "u", "a", None))
    runmod.run(None, dry_run=False)
    postings.clear(); runmod.run(None, dry_run=False)
    postings.append(Job("Acme", "9", "ML Engineer", ["Pune, India"], "u", "a", None)); runmod.run(None, dry_run=False)
    assert pushed == ["Acme: ML Engineer"]


def test_not_live_retries_then_dead(env):
    tmp, postings, pushed, live = env
    runmod.run(None, dry_run=False)
    live["ok"] = False
    postings.append(Job("Acme", "5", "ML Engineer", ["Pune, India"], "u", "a", None))
    for _ in range(3):
        runmod.run(None, dry_run=False)
    assert pushed == [] and seen(tmp)["5"]["status"] == "dead"


def test_dry_run_writes_nothing(env):
    tmp, *_ = env
    runmod.run(None, dry_run=True)
    assert not (tmp / "seen.json").exists()


def test_fetch_failure_is_visible(env, monkeypatch):
    tmp, *_ = env
    monkeypatch.setitem(runmod.FETCHERS, "fake", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    runmod.run(None, dry_run=False)
    h = json.loads((tmp / "health.json").read_text())["Acme"]
    assert h["ok"] is False and h["fails"] == 1 and "ConnectError" in h["error"]


def test_workday_rechecks_location_after_detail(env, monkeypatch):
    tmp, postings, pushed, _ = env
    monkeypatch.setattr(runmod, "load_boards", lambda names: [{"name": "Acme", "ats": "workday", "locator": ["x"]}])
    monkeypatch.setitem(runmod.FETCHERS, "workday", lambda c, b, **k: list(postings))
    details = {"7": ["Noida, India"], "8": ["Noida, India", "Pune, India"]}
    monkeypatch.setattr(runmod, "detail_hooks", lambda ats: (lambda c, j: setattr(j, "locations", details[j.id]),
                                                            lambda c, j: (True, "ok")) if ats == "workday" else None)
    runmod.run(None, dry_run=False)  # baseline
    postings += [Job("Acme", i, "ML Engineer", [], "u", "a", None) for i in ("7", "8")]  # "2 Locations"
    runmod.run(None, dry_run=False)
    s = seen(tmp)
    assert s["7"]["status"] == "filtered" and s["8"]["status"] == "alerted" and pushed == ["Acme: ML Engineer"]


def amazon_job(n, date="November 20, 2026", locs=("Hyderabad", "Telangana")):
    loc = json.dumps({"normalizedCityName": locs[0], "normalizedStateName": locs[1], "normalizedCountryName": "India"})
    return {"id_icims": str(n), "title": " Applied Scientist ", "job_path": f"/en/jobs/{n}/applied-scientist",
            "url_next_step": f"https://account.amazon.jobs/jobs/{n}/apply", "posted_date": date,
            "locations": [loc], "city": locs[0]}


def amazon_client(total):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.params["country"] == "IND" and req.url.params["sort"] == "recent"
        off = int(req.url.params["offset"])
        return httpx.Response(200, json={"hits": total, "jobs": [amazon_job(i) for i in range(off, min(off + 100, total))]})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_amazon_parse(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c, calls = amazon_client(150)
    jobs = sources.amazon(c, "Amazon")
    assert len(jobs) == 150 and len(calls) == 2
    j = jobs[0]
    assert (j.id, j.title, j.locations, j.posted_at) == ("0", "Applied Scientist", ["Hyderabad, Telangana, India"], "2026-11-20")
    assert j.url == "https://www.amazon.jobs/en/jobs/0/applied-scientist"
    assert j.apply_url == "https://account.amazon.jobs/jobs/0/apply"
    assert PF.location_ok(j.locations[0])


def test_amazon_incremental_stops_when_page_has_nothing_new(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c, calls = amazon_client(2000)
    sources.amazon(c, "Amazon", skip={str(i) for i in range(5, 2000)}, full=False)
    assert len(calls) == 2  # page 1 has new IDs, page 2 has none


def test_custom_board_routed_by_platform():
    assert runmod.kind({"ats": "custom", "platform": "amazon"}) == "amazon"
    assert runmod.kind({"ats": "workday"}) == "workday"


def test_signin_apply_link_checks_first_hop_only():
    def handler(req):
        assert req.url.host == "account.amazon.jobs"  # never follows into the SSO chain
        return httpx.Response(302, headers={"location": "https://idp.federate.amazon.com/sso"})
    c = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    ok, why = runmod.live(c, "https://account.amazon.jobs/jobs/1/apply")
    assert ok and "sign-in" in why


def test_jibe_parse_and_pages():
    def handler(req):
        assert req.url.params["location"] == "India"
        page = int(req.url.params["page"])
        rows = [{"data": {"slug": str(100 * page + i), "req_id": str(100 * page + i), "title": " Data Scientist ",
                          "full_location": "Pune, India", "country": "India", "posted_date": "2026-10-07T10:39:00+0000",
                          "posting_expiry_date": "2026-12-30T18:30:00+0000",
                          "apply_url": f"https://careers-here.icims.com/jobs/{100 * page + i}/login"}}
                for i in range(100 if page == 1 else 20)]
        return httpx.Response(200, json={"totalCount": 120, "jobs": rows})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = sources.jibe(c, "HERE", "https://careers.here.com/api/jobs")
    assert len(jobs) == 120
    j = jobs[0]
    assert j.url == "https://careers.here.com/jobs/100" and j.apply_url == "https://careers-here.icims.com/jobs/100/login"
    assert j.posted_at == "2026-10-07T10:39:00+00:00" and j.closes_at == "2026-12-30T18:30:00+00:00"
    assert j.locations == ["Pune, India"] and PF.check(j)[0]


def test_atlassian_keeps_india_only():
    rows = [{"id": "1", "title": " Machine Learning Engineer ", "locations": ["Bengaluru - India -   Bengaluru,  560071 India", "Sydney - Australia"],
             "portalJobPost": {"portalUrl": "https://careers-apac-atlassian.icims.com/jobs/1/ml/job"},
             "applyUrl": "https://careers-apac-atlassian.icims.com/jobs/1/ml/job?mode=apply"},
            {"id": "2", "title": "ML Engineer", "locations": ["Sydney - Australia"], "portalJobPost": {}, "applyUrl": "x"}]
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows)))
    [j] = sources.atlassian(c, "Atlassian")
    assert j.id == "1" and j.locations == ["Bengaluru - India - Bengaluru, 560071 India"] and j.posted_at is None
    assert j.apply_url.endswith("?mode=apply") and PF.check(j)[0]


# ---------------------------------------------------------------- JD keyword pass (title misses)

QUALS_JD = ("About the team: we are building the future of GenAI and AI agents across Amazon.\n"
            "Basic Qualifications\n- 3+ years of Python\n- Experience building LLM applications with RAG\n")


def test_jd_score_counts_distinct_signals_from_the_qualifications_on():
    s, hits = PF.jd_score(QUALS_JD)
    assert "LLM" in hits and "RAG" in hits and "GenAI" not in hits and "agents" not in hits  # blurb not scored
    assert s >= PF.jd_threshold
    assert PF.jd_score(QUALS_JD + "\nLLM LLM LLM")[0] == s  # repeats count once
    blurb_only = "We are a GenAI company building AI agents.\nRequirements\n- 3 years of Java and Spring Boot\n"
    assert PF.jd_score(blurb_only)[0] < PF.jd_threshold


def test_jd_score_without_a_heading_scores_the_whole_text():
    assert PF.jd_score("Build LLM agents with LangChain and RAG.")[0] >= PF.jd_threshold


@pytest.mark.parametrize("title,locs,ok", [
    ("Software Engineer II", ("Bengaluru, India",), True),
    ("Lead Software Engineer", ("Mumbai, India",), True),
    ("Research Analyst", (), True),                      # location unknown: kept
    ("ML Engineer", ("Bengaluru, India",), False),       # the title already passes: no JD read needed
    ("Software Engineer II", ("Noida, India",), False),  # North India
    ("Software Engineering Intern", ("Pune, India",), False),
    ("Director, Software Engineering", ("Pune, India",), False),
    ("Enterprise Architect (Martech)", ("Pune, India",), False),  # a function the spec never takes
    ("Senior Engineering Manager", ("Pune, India",), False),
])
def test_jd_eligible(title, locs, ok):
    assert PF.jd_eligible(job(title, locs)) is ok


def _jd_job(i, title, text):
    j = Job("Acme", i, title, ["Pune, India"], "u", "a", None)
    j.jd = text
    return j


def test_title_miss_with_genai_jd_is_alerted(env):
    tmp, postings, pushed, _ = env
    runmod.run(None, dry_run=False)  # baseline
    postings += [_jd_job("1", "Software Engineer II", QUALS_JD),
                 _jd_job("2", "Software Engineer III", "Requirements\n- Java, Spring Boot, Kafka\n")]
    runmod.run(None, dry_run=False)
    s = seen(tmp)
    assert pushed == ["Acme: Software Engineer II"]
    assert s["1"]["status"] == "alerted" and s["1"]["via"] == "jd" and s["1"]["jd_score"] >= PF.jd_threshold
    assert s["2"]["status"] == "filtered" and s["2"]["jd_score"] < PF.jd_threshold


def test_jd_pass_unreadable_jd_retries_then_records_unknown(env, monkeypatch):
    tmp, postings, pushed, _ = env
    monkeypatch.setattr(runmod.jd, "get", lambda c, j: ("", "unknown (JD fetch failed: ConnectError)"))
    runmod.run(None, dry_run=False)
    postings.append(Job("Acme", "4", "Software Engineer II", ["Pune, India"], "u", "a", None))
    runmod.run(None, dry_run=False)
    assert seen(tmp)["4"]["status"] == "jdcheck" and seen(tmp)["4"]["tries"] == 1
    runmod.run(None, dry_run=False); runmod.run(None, dry_run=False)
    assert seen(tmp)["4"]["status"] == "filtered" and seen(tmp)["4"]["jd_score"] == "unknown" and pushed == []


def test_jd_pass_cap_defers_the_rest(env, monkeypatch):
    tmp, postings, pushed, _ = env
    from dataclasses import replace
    capped = replace(Prefilter.load(), jd_max_per_pass=1)
    monkeypatch.setattr(Prefilter, "load", classmethod(lambda cls, *a: capped))
    runmod.run(None, dry_run=False)
    postings += [_jd_job(str(i), "Software Engineer II", QUALS_JD) for i in (1, 2)]
    runmod.run(None, dry_run=False)
    assert sorted(e["status"] for e in seen(tmp).values()) == ["alerted", "jdcheck"]
    runmod.run(None, dry_run=False)  # the deferred one is read on the next pass
    assert [e["status"] for e in seen(tmp).values()] == ["alerted", "alerted"]


def test_applied_roles_pass_the_title_filter():
    """Regression: every role marked APPLIED in the private ledger must reach the screener by its title
    (or at least get its JD read). Skipped where there is no private/ (Actions, a fresh clone)."""
    from pathlib import Path
    led = Path(__file__).parents[1] / "private" / "ledger.json"
    if not led.exists():
        pytest.skip("no private ledger")
    roles = [e for e in json.loads(led.read_text(encoding="utf-8"))["roles"].values() if e.get("status") == "APPLIED"]
    dropped = [e["title"] for e in roles
               if not PF.check(job(e["title"]))[0] and not PF.jd_eligible(job(e["title"]))]
    assert dropped == []
