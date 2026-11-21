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
