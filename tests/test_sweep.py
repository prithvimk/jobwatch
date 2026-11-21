"""Sweep: counts, dropped title hits with reasons, history labels, screening. Fetcher and Claude are faked."""
import json

import pytest

from detector import ledger, screen, state, sweep
from detector.prefilter import Prefilter
from detector.sources import Job

PF = Prefilter.load()


def J(jid, title, locs=("Pune, India",), req=None, jd="Requires 3+ years of experience in Python and ML."):
    return Job("Acme", jid, title, list(locs), f"https://jd/{jid}", f"https://apply/{jid}", None, req_id=req, jd=jd)


POSTINGS = [
    J("1", "ML Engineer"),
    J("2", "GenAI Engineer", ["Gurugram, India"]),           # title hit, north India
    J("3", "Principal Machine Learning Scientist"),          # title hit, excluded word
    J("4", "Accountant"),                                     # other title
    J("5", "Data Scientist", ["London, UK"]),                 # outside India
    J("6", "Applied Scientist", [], req="R-6"),               # no location stated: kept
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "private").mkdir()
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(screen, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(screen, "KITS", tmp_path / "private" / "kits")
    monkeypatch.setattr(screen, "VERDICTS_FILE", tmp_path / "private" / "verdicts.json")
    (tmp_path / "private" / "answers.md").write_text("facts")
    monkeypatch.setattr(sweep, "all_boards", lambda: [{"name": "Acme", "ats": "fake"}, {"name": "Acme Labs", "ats": "fake"},
                                                      {"name": "Google", "ats": "custom", "platform": "google-careers",
                                                       "effort": "custom-scrape"}])
    calls = []
    monkeypatch.setitem(sweep.FETCHERS, "fake", lambda c, b, **k: calls.append(k) or [j for j in POSTINGS])
    return tmp_path, calls


def test_counts_candidates_and_dropped_hits(env):
    tmp, calls = env
    [r] = sweep.run(["Acme"], out_dir=tmp / "out")
    assert calls[0]["full"] is True and calls[0]["skip"] == set()
    assert calls[0]["location_ok"]("Noida, India")  # north India is enumerated so it can be reported
    assert (r.fetched, r.india, r.outside, r.no_location, r.other_titles) == (6, 5, 1, 1, 1)
    assert [c.job.id for c in r.candidates] == ["1", "6"]
    reasons = {j.id: why for j, why in r.dropped_hits}
    assert "North India" in reasons["2"] and "principal" in reasons["3"].lower()
    text = (next((tmp / "out").glob("*-Acme.md"))).read_text(encoding="utf-8")
    assert "- India: 5 (1 with no location stated)" in text
    assert "## Title hits dropped by the prefilter" in text and "Accountant" not in text
    assert 'JD says: "Requires 3+ years of experience in Python and ML"' in text


def test_resolve_exact_then_substring():
    boards = [{"name": "Acme"}, {"name": "Acme Labs"}, {"name": "Visa"}]
    assert [b["name"] for b in sweep.resolve("acme", boards)] == ["Acme"]
    assert [b["name"] for b in sweep.resolve("labs", boards)] == ["Acme Labs"]
    assert sweep.resolve("nope", boards) == []


def test_unwatched_and_unknown_boards_are_visible(env):
    tmp, _ = env
    r1, r2 = sweep.run(["Google", "Nope"], out_dir=tmp / "out")
    assert r1.error.startswith("unknown: no fetcher") and r2.error.startswith("unknown: no board")
    assert not (tmp / "out").exists()


def test_history_labels(env):
    tmp, _ = env
    seen = {"boards": {"Acme": {"jobs": {"1": {"first_seen": "2026-11-21T10:00:00+00:00", "status": "baseline"}}}}}
    led = {"roles": {"Acme:9": {"board": "Acme", "id": "9", "req": "R-6", "status": "SKIPPED"},
                     "Acme:7": {"board": "Acme", "id": "7", "status": "SHOWN", "shown_at": "2026-11-21T10:00:00+00:00"}}}
    assert sweep.history(J("1", "x"), seen, led).startswith("open since before tracking")
    assert sweep.history(J("6", "x", req="R-6"), seen, led).startswith("repost of a SKIPPED")
    assert sweep.history(J("7", "x"), seen, led).startswith("alerted 21 Nov")
    assert sweep.history(J("8", "x"), seen, led) == "new: not seen by the detector yet"


def test_screen_reuses_cache_respects_limit_and_ledger(env, monkeypatch):
    tmp, _ = env
    (tmp / "private" / "verdicts.json").write_text(json.dumps({"Acme:1": {
        "verdict": "IN", "fit": 4, "location": "Pune", "experience_asked": "3+", "one_line": "Cached.", "gaps": [],
        "out_reason": "", "resume_tweak": "", "skills": []}}))
    monkeypatch.setattr(screen, "available", lambda: (True, "ok"))
    calls = []
    monkeypatch.setattr(screen, "screen", lambda job, text, src: calls.append(job.id) or {
        "verdict": "STRETCH", "fit": 3, "location": "India", "experience_asked": "5+", "one_line": "New.",
        "gaps": ["PhD"], "out_reason": "", "resume_tweak": "", "skills": []})
    [r] = sweep.run(["Acme"], screen_roles=True, out_dir=tmp / "out")
    assert calls == ["6"]  # 1 was cached
    assert [c.verdict["verdict"] for c in r.candidates] == ["IN", "STRETCH"]
    assert len(list((tmp / "private" / "kits").glob("*.md"))) == 1
    assert "Acme:6" in json.loads((tmp / "private" / "verdicts.json").read_text())

    # an APPLIED role is never screened; over the limit is named, not dropped
    d = ledger.load(); ledger.mark(d, "Acme:6", "APPLIED"); ledger.save(d)
    (tmp / "private" / "verdicts.json").write_text("{}")
    calls.clear()
    [r] = sweep.run(["Acme"], screen_roles=True, screen_limit=0, out_dir=tmp / "out")
    assert calls == []
    notes = {c.job.id: c.note for c in r.candidates}
    assert notes["1"] == "not screened (over --screen-limit 0)" and notes["6"] == ""


def test_detail_failure_and_relocation_after_detail(env, monkeypatch):
    tmp, _ = env
    def enrich(c, job):
        if job.id == "1":
            raise RuntimeError("closed")
        job.locations = ["Noida, India"]
    monkeypatch.setattr(sweep, "detail_hooks", lambda ats: (enrich, None))
    [r] = sweep.run(["Acme"], out_dir=tmp / "out")
    assert r.candidates == [] and [j.id for j, _ in r.not_open] == ["1"]
    assert any(j.id == "6" and "after detail" in why for j, why in r.dropped_hits)


@pytest.mark.parametrize("text,quote", [
    ("We want 5-8 years of ML experience. Also Python.", "We want 5-8 years of ML experience"),
    ("Minimum 3 yrs in NLP", "Minimum 3 yrs in NLP"),
    ("No experience bar here.", ""),
    ("x" * 80 + " and certification on software concepts and 5+ years applied experience in Python software development",
     "and certification on software concepts and 5+ years applied experience in Python software development"),
])
def test_years_quote_is_verbatim(text, quote):
    assert sweep.years_quote(text) == quote
