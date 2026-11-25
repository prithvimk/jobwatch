"""Screener: prompt assembly, reply parsing, and how verdicts drive alerts (Claude is faked)."""
import json
from datetime import datetime

import pytest

from detector import run as runmod
from detector import screen, state
from detector.sources import Job

V_IN = {"verdict": "IN", "fit": 4, "location": "Pune", "experience_asked": "3+ years", "one_line": "LLM agents match.",
        "gaps": ["Spark"], "out_reason": "", "resume_tweak": "Lead with LangGraph."}


def item(req, status="shown", hard=False, evidence=""):
    return {"req": req, "status": status, "hard": hard, "evidence": evidence}


def test_parse_takes_first_json_object_and_computes_fit():
    reply = ('Here you go:\n{"verdict": "stretch", "fit": 5, "location": "Mumbai", "required": ['
             '{"req": "Python", "status": "Shown", "hard": false, "evidence": "Python, 3 yrs"},'
             '{"req": "PyTorch", "status": "partial", "hard": "false"},'
             '{"req": "Spark", "status": "missing"},'
             '{"req": "bad item", "status": "maybe"}, "not a dict"]} trailing')
    v = screen.parse(reply)
    assert v["verdict"] == "STRETCH" and v["one_line"] == ""
    assert [x["req"] for x in v["required"]] == ["Python", "PyTorch", "Spark"]  # bad items dropped
    assert v["coverage"] == 0.5 and v["fit"] == 2  # Claude's own "fit" is ignored: (1 + 0.5 + 0) / 3
    assert v["gaps"] == ["Spark", "partial: PyTorch"]


@pytest.mark.parametrize("marks,fit", [
    (["shown"] * 10, 5),
    (["shown"] * 9 + ["missing"], 5),          # 0.90
    (["shown"] * 8 + ["partial"] * 2, 5),      # 0.90
    (["shown"] * 8 + ["missing"] * 2, 4),      # 0.80
    (["shown"] * 3 + ["missing"], 4),          # 0.75
    (["shown"] * 6 + ["missing"] * 4, 3),      # 0.60
    (["shown", "missing"], 2),                 # 0.50
    (["shown"] * 2 + ["missing"] * 4, 1),      # 0.33
    (["missing"] * 3, 1),
])
def test_fit_bands(marks, fit):
    v = screen.score({"verdict": "IN", "required": [item(f"r{i}", m) for i, m in enumerate(marks)]})
    assert v["fit"] == fit


def test_unmet_hard_bar_caps_fit_and_leads_gaps():
    req = [item(f"r{i}") for i in range(9)]
    v = screen.score({"verdict": "IN", "required": req + [item("PhD only", "missing", hard=True), item("Spark", "missing")]})
    assert v["fit"] == 2 and v["gaps"] == ["Hard bar: PhD only", "Spark"]
    v = screen.score({"verdict": "IN", "required": req + [item("3 yrs people management", "partial", hard=True)]})
    assert v["fit"] == 3 and v["gaps"] == ["Hard bar: 3 yrs people management (partly shown)"]
    v = screen.score({"verdict": "IN", "required": [item("Master's", "shown", hard=True)]})
    assert v["fit"] == 5  # a hard bar that is met costs nothing


def test_parse_reads_renamed_fields_and_notes_unreadable_items():
    v = screen.parse('{"verdict": "IN", "required": [{"requirement": "Python", "status": "Met"},'
                     '{"requirement": "SQL", "status": "partially shown"}, {"status": "shown"}]}')
    assert [(x["req"], x["status"]) for x in v["required"]] == [("Python", "shown"), ("SQL", "partial")]
    assert v["checklist_note"] == "1 of 3 required items unreadable"
    v = screen.parse('{"verdict": "IN", "required": [{"what": "Python", "status": "shown"}]}')
    assert v["fit"] is None and screen.fit_text(v) == "fit ?/5 (1 of 1 required items unreadable)"


def test_only_years_and_degree_required_scores_preferred():
    v = screen.score({"verdict": "STRETCH", "required": [], "preferred": [item("Python"), item("AML", "missing")]})
    assert v["fit_basis"] == "preferred" and v["coverage"] == 0.5 and v["fit"] == 2 and v["gaps"] == ["AML"]
    assert screen.fit_text(v) == "fit 2/5 (1 of 2 preferred)"


def test_out_is_fit_1_and_no_requirements_is_unknown():
    assert screen.score({"verdict": "OUT", "required": []})["fit"] == 1
    v = screen.score({"verdict": "IN", "required": []})
    assert v["fit"] is None and v["coverage"] is None
    assert screen.fit_text(v) == "fit ?/5 (requirements unknown)"


def test_fit_text_and_old_verdicts():
    v = screen.score({"verdict": "IN", "required": [item("a"), item("b", "partial"), item("c", "missing")]})
    assert screen.fit_text(v) == "fit 2/5 (1 of 3 required, 1 partial)"
    assert screen.fit_text({"verdict": "IN", "fit": 4}) == "fit 4/5"  # cached before the checklist
    assert screen.fit_text(None) == "not screened"


def test_rank_fit_then_in_before_stretch_then_coverage():
    a = {"verdict": "STRETCH", "fit": 4, "coverage": 0.85}
    b = {"verdict": "IN", "fit": 4, "coverage": 0.76}
    c = {"verdict": "IN", "fit": 3, "coverage": 0.7}
    d = {"verdict": "IN", "fit": 4, "coverage": 0.8}
    old = {"verdict": "IN", "fit": 4}
    unknown = {"verdict": "IN", "fit": None}
    order = sorted([c, a, b, d, old, unknown, None], key=screen.rank, reverse=True)
    assert order == [d, b, old, a, c, unknown, None]


def test_kit_has_checklist():
    v = screen.score({"verdict": "IN", "location": "Pune", "experience_asked": "3+", "one_line": "x",
                      "resume_tweak": "", "required": [item("Python", evidence="Python, 3 yrs"),
                                                       item("PhD", "missing", hard=True)],
                      "preferred": [item("Kafka", "missing")]})
    text = screen.checklist(v)
    assert "## Required" in text and "| ✓ | Python | Python, 3 yrs |" in text and "| ✗ | PhD (hard bar) |  |" in text
    assert "## Preferred" in text and "Kafka" in text


@pytest.mark.parametrize("reply", ["no json here", '{"verdict": "MAYBE"}', '{"verdict": "IN", "fit": '])
def test_parse_rejects_bad_replies(reply):
    with pytest.raises(screen.ScreenError):
        screen.parse(reply)


def test_prompt_carries_spec_resume_facts_and_jd(tmp_path, monkeypatch):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "resume.md").write_text("RESUME-TEXT", encoding="utf-8")
    (tmp_path / "private" / "answers.md").write_text("FACTS-TEXT", encoding="utf-8")
    (tmp_path / "CLAUDE.local.md").write_text("# x\n\n## User\nU\n\n## Filter spec (screener)\n\n- SPEC-LINE\n", encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "screen.md").write_text((screen.ROOT / "prompts" / "screen.md").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(screen, "ROOT", tmp_path)
    monkeypatch.setattr(screen, "PRIVATE", tmp_path / "private")
    job = Job("Acme", "1", "ML Engineer", ["Pune, India"], "u", "a", None)
    p = screen.build_prompt(job, "JD-TEXT", "ATS API")
    for s in ("RESUME-TEXT", "FACTS-TEXT", "- SPEC-LINE", "JD-TEXT", "Company: Acme", "Listed locations: Pune, India"):
        assert s in p
    assert "U\n" not in p.split("## Filter spec")[1].split("## Candidate resume")[0]  # only the spec section


def test_digest_once_a_day_after_8pm(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "DIGEST", tmp_path / "digest.json")
    job = Job("Acme", "1", "Data Engineer", ["Pune"], "u", "a", None)
    screen.queue_out(job, {**V_IN, "verdict": "OUT", "out_reason": "Track: data-engineering role, pipelines and ETL.",
                           "out_short": "data-engineering track"})
    evening = datetime(2026, 11, 22, 20, 5, tzinfo=screen.IST)
    assert not screen.digest_due(datetime(2026, 11, 22, 19, 0, tzinfo=screen.IST))[0]
    due, d = screen.digest_due(evening)
    assert due and screen.digest_text(d) == "Acme · Data Engineer\ndata-engineering track"
    screen.mark_digest_sent(d, evening)
    assert not screen.digest_due(evening)[0]


# ---------------------------------------------------------------- run: verdicts drive alerts

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(screen, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(screen, "KITS", tmp_path / "private" / "kits")
    monkeypatch.setattr(screen, "DIGEST", tmp_path / "private" / "digest.json")
    monkeypatch.setattr(screen, "VERDICTS_FILE", tmp_path / "private" / "verdicts.json")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "answers.md").write_text("facts", encoding="utf-8")
    monkeypatch.setattr(runmod, "load_boards", lambda names: [{"name": "Acme", "ats": "fake", "locator": ["x"]}])
    monkeypatch.setattr(runmod, "liveness", lambda c, j: (True, "test"))
    monkeypatch.setattr(runmod.jd, "get", lambda c, j: ("jd text", "test"))
    pushed = []
    monkeypatch.setattr(runmod.notify, "push", lambda title, body, **k: pushed.append((title, body, k.get("priority"))) or True)
    postings = []
    monkeypatch.setitem(runmod.FETCHERS, "fake", lambda c, b, **k: list(postings))
    runmod.run(None, dry_run=False, screen_roles=True)  # baseline
    return tmp_path, postings, pushed


def seen(tmp):
    return json.loads((tmp / "seen.json").read_text())["boards"]["Acme"]["jobs"]


def test_verdicts_order_alerts_and_out_goes_to_digest(env, monkeypatch):
    tmp, postings, pushed = env
    replies = {"1": {**V_IN, "fit": 2, "verdict": "STRETCH"}, "2": V_IN,
               "3": {**V_IN, "verdict": "OUT", "fit": 1, "out_reason": "Java full-stack track"}}
    calls = []
    monkeypatch.setattr(screen, "screen", lambda job, text, src: calls.append(job.id) or dict(replies[job.id]))
    postings += [Job("Acme", i, t, ["Pune, India"], "u", "a", None) for i, t in
                 (("1", "ML Engineer"), ("2", "GenAI Engineer"), ("3", "AI Engineer"))]
    runmod.run(None, dry_run=False, screen_roles=True)
    assert [t for t, *_ in pushed] == ["IN Acme: GenAI Engineer", "STRETCH Acme: ML Engineer"]  # best fit first
    assert pushed[0][2] == 4 and "fit 4/5 · Pune" in pushed[0][1] and "Gaps: Spark" in pushed[0][1]
    s = seen(tmp)
    assert s["3"]["status"] == "screened" and s["1"]["status"] == s["2"]["status"] == "alerted"
    assert "Java full-stack track" in (tmp / "private" / "digest.json").read_text()
    assert len(list((tmp / "private" / "kits").glob("*.md"))) == 2
    runmod.run(None, dry_run=False, screen_roles=True)
    assert sorted(calls) == ["1", "2", "3"]  # never re-screened


def test_screening_failure_sends_plain_alert_with_reason(env, monkeypatch):
    tmp, postings, pushed = env
    def boom(job, text, src):
        raise screen.ScreenError("claude error: Not logged in")
    monkeypatch.setattr(screen, "screen", boom)
    postings.append(Job("Acme", "9", "ML Engineer", ["Pune, India"], "u", "a", None))
    runmod.run(None, dry_run=False, screen_roles=True)
    [(title, body, _)] = pushed
    assert title == "Acme: ML Engineer" and "Screening unavailable: claude error: Not logged in" in body
    assert seen(tmp)["9"]["status"] == "alerted"


def test_cached_verdict_used_when_push_retries(env, monkeypatch):
    tmp, postings, pushed = env
    calls = []
    monkeypatch.setattr(screen, "screen", lambda job, text, src: calls.append(1) or dict(V_IN))
    ok = {"v": False}
    monkeypatch.setattr(runmod.notify, "push", lambda title, body, **k: (pushed.append(title) or True) if ok["v"] else False)
    postings.append(Job("Acme", "5", "ML Engineer", ["Pune, India"], "u", "a", None))
    runmod.run(None, dry_run=False, screen_roles=True)  # push fails: stays pending
    assert seen(tmp)["5"]["status"] == "pending"
    ok["v"] = True
    runmod.run(None, dry_run=False, screen_roles=True)
    assert calls == [1] and pushed == ["IN Acme: ML Engineer"]


LONG = ("Seniority: required floor is 7+ years, which is 6+ and therefore OUT. Track: required qualifications "
        "are compliance, audit and risk management, not GenAI/ML or data science.")


def test_digest_layout_and_fallback_for_entries_without_short():
    d = {"pending": [{"board": "Amazon", "title": "Senior Risk Manager, AI Assurance & Inspections, FORGE, Hyderabad Office", "reason": LONG},
                     {"board": "Teradata", "title": "Staff Data Scientist", "reason": LONG, "short": "8+ yrs"}]}
    blocks = screen.digest_text(d).split("\n\n")
    assert len(blocks) == 2 and blocks[1] == "Teradata · Staff Data Scientist\n8+ yrs"
    title, reason = blocks[0].split("\n")
    assert len(title) <= len("Amazon · ") + 60 and title.endswith("…")
    assert len(reason) <= 80 and reason.startswith("Seniority: required floor is 7+ years") and reason.endswith("…")


@pytest.mark.parametrize("n", [1, 17, 18, 40, 200])
def test_digest_stays_a_message_under_ntfy_limit(n):
    rows = [{"board": "Amazon", "title": f"Senior Risk Manager, AI Assurance & Inspections {i}", "reason": LONG}
            for i in range(n)]
    text = screen.digest_text({"pending": rows})
    assert len(text.encode()) <= 4096
    shown = text.count("Amazon · ")
    if shown < n:
        assert text.endswith(f"…and {n - shown} more (full reasons on the status page)")
    else:
        assert "more (full" not in text
