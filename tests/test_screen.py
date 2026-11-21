"""Screener: prompt assembly, reply parsing, and how verdicts drive alerts (Claude is faked)."""
import json
from datetime import datetime

import pytest

from detector import run as runmod
from detector import screen, state
from detector.sources import Job

V_IN = {"verdict": "IN", "fit": 4, "location": "Pune", "experience_asked": "3+ years", "one_line": "LLM agents match.",
        "gaps": ["Spark"], "out_reason": "", "resume_tweak": "Lead with LangGraph."}


def test_parse_takes_first_json_object_and_clamps():
    v = screen.parse('Here you go:\n{"verdict": "stretch", "fit": 9, "gaps": ["PhD"], "location": "Mumbai"} trailing')
    assert v["verdict"] == "STRETCH" and v["fit"] == 5 and v["gaps"] == ["PhD"] and v["one_line"] == ""


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
    screen.queue_out(job, {**V_IN, "verdict": "OUT", "out_reason": "data-engineering track"})
    evening = datetime(2026, 11, 22, 20, 5, tzinfo=screen.IST)
    assert not screen.digest_due(datetime(2026, 11, 22, 19, 0, tzinfo=screen.IST))[0]
    due, d = screen.digest_due(evening)
    assert due and "Acme: Data Engineer (data-engineering track)" in screen.digest_text(d)
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
