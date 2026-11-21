"""Ledger: SHOWN on push, APPLIED/SKIPPED from phone taps or the CLI, reposts of resolved roles suppressed."""
import json

import httpx
import pytest

from detector import ledger, state
from detector import run as runmod
from detector.sources import Job


def J(jid, req=None, title="ML Engineer", closes=None):
    return Job("Acme Bank", jid, title, ["Pune, India"], f"u{jid}", f"a{jid}", None, req_id=req, closes_at=closes)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    (tmp_path / "private").mkdir()
    monkeypatch.setattr(runmod, "load_boards", lambda names: [{"name": "Acme Bank", "ats": "fake", "locator": ["x"]}])
    monkeypatch.setattr(runmod, "liveness", lambda c, j: (True, "test"))
    pushed = []
    monkeypatch.setattr(runmod.notify, "push", lambda title, body, **k: pushed.append((title, body, k.get("actions"))) or True)
    postings = []
    monkeypatch.setitem(runmod.FETCHERS, "fake", lambda c, b, **k: list(postings))
    runmod.run(None, dry_run=False)  # baseline
    return tmp_path, postings, pushed


def seen(tmp):
    return json.loads((tmp / "state" / "seen.json").read_text())["boards"]["Acme Bank"]["jobs"]


def test_push_records_shown_with_req_and_closing_date(env):
    tmp, postings, pushed = env
    postings.append(J("1", req="R-77", closes="2026-12-01"))
    runmod.run(None, dry_run=False)
    e = ledger.load()["roles"]["Acme Bank:1"]
    assert e["status"] == "SHOWN" and e["req"] == "R-77" and e["closes_at"] == "2026-12-01" and e["title"] == "ML Engineer"


def test_dry_run_and_no_private_dir_write_no_ledger(env, tmp_path, monkeypatch):
    _, postings, pushed = env
    postings.append(J("1"))
    runmod.run(None, dry_run=True)
    assert pushed and not ledger.LEDGER_FILE.exists()
    monkeypatch.setattr(ledger, "PRIVATE", tmp_path / "absent")  # Actions: no private/
    runmod.run(None, dry_run=False)
    assert seen(tmp_path)["1"]["status"] == "alerted" and not (tmp_path / "absent").exists()


@pytest.mark.parametrize("status", ["APPLIED", "SKIPPED"])
def test_repost_of_resolved_role_is_suppressed(env, status):
    tmp, postings, pushed = env
    postings.append(J("1", req="R-77"))
    runmod.run(None, dry_run=False)
    d = ledger.load(); ledger.mark(d, "Acme Bank:1", status); ledger.save(d)
    postings[:] = [J("2", req="R-77"), J("3", req="R-78", title="GenAI Engineer")]
    runmod.run(None, dry_run=False)
    assert [t for t, *_ in pushed] == ["Acme Bank: ML Engineer", "Acme Bank: GenAI Engineer"]
    assert seen(tmp)["2"]["status"] == "repost"
    assert ledger.load()["roles"]["Acme Bank:1"]["status"] == status  # never downgraded


def test_repost_of_shown_role_is_alerted_with_note(env):
    tmp, postings, pushed = env
    postings.append(J("1", req="R-77"))
    runmod.run(None, dry_run=False)
    postings[:] = [J("2", req="R-77")]
    runmod.run(None, dry_run=False)
    assert len(pushed) == 2 and "Repost: same requisition first shown" in pushed[1][1]


def test_buttons_post_to_reply_topic(env, monkeypatch):
    _, postings, pushed = env
    monkeypatch.setenv("NTFY_TOPIC", "secret")
    postings.append(J("1"))
    runmod.run(None, dry_run=False)
    acts = pushed[0][2]
    assert [a["label"] for a in acts] == ["Apply", "Applied", "Skip"]
    assert acts[1]["url"] == "https://ntfy.sh/secret-reply" and acts[1]["body"] == "applied Acme Bank:1"
    assert acts[2]["body"] == "skipped Acme Bank:1" and acts[2]["method"] == "POST"


def test_without_topic_buttons_fall_back_to_apply_and_jd():
    assert [a["label"] for a in ledger.actions(J("1"))] == ["Apply", "JD"]


def test_pull_applies_phone_taps_and_remembers_position(tmp_path, monkeypatch):
    (tmp_path / "private").mkdir()
    monkeypatch.setenv("NTFY_TOPIC", "secret")
    asked = []
    lines = [{"event": "open", "id": "o"},
             {"event": "message", "id": "m1", "time": 1795000000, "message": "applied Bank of America:R-1"},
             {"event": "message", "id": "m2", "time": 1795000100, "message": "hello"},
             {"event": "message", "id": "m3", "time": 1795000200, "message": "skipped Acme Bank:9"}]

    def handler(req):
        asked.append(str(req.url))
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert ledger.pull(c) == [("Bank of America:R-1", "APPLIED"), ("Acme Bank:9", "SKIPPED")]
    d = ledger.load()
    assert d["roles"]["Bank of America:R-1"] == {"board": "Bank of America", "id": "R-1", "status": "APPLIED",
                                                 "marked_at": "2026-11-18T11:06:40+00:00"}
    assert d["reply_since"] == "m3" and "secret-reply/json" in asked[0] and "since=12h" in asked[0]
    ledger.pull(c)
    assert "since=m3" in asked[1]


def test_pull_failure_is_printed_not_raised(tmp_path, monkeypatch, capsys):
    (tmp_path / "private").mkdir()
    monkeypatch.setenv("NTFY_TOPIC", "secret")
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert ledger.pull(c) == [] and "reading phone replies failed" in capsys.readouterr().out


def test_find_by_key_id_req_and_state():
    d = {"roles": {"Acme Bank:1": {"board": "Acme Bank", "id": "1", "req": "R-77", "status": "SHOWN"},
                   "Zeta:1": {"board": "Zeta", "id": "1", "status": "SHOWN"}}}
    seen = {"boards": {"Visa": {"jobs": {"744000": {"req": "REF-5"}}}}}
    assert ledger.find(d, "Acme Bank:1", seen) == ["Acme Bank:1"]
    assert ledger.find(d, "r-77", seen) == ["Acme Bank:1"]
    assert sorted(ledger.find(d, "1", seen)) == ["Acme Bank:1", "Zeta:1"]  # ambiguous
    assert ledger.find(d, "REF-5", seen) == ["Visa:744000"]  # alerted from Actions, not in the ledger yet
    assert ledger.find(d, "nope", seen) == []


def test_cli_marks_and_lists(tmp_path, monkeypatch, capsys):
    (tmp_path / "private").mkdir()
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(runmod, "ROOT", tmp_path)  # load_env reads no real ntfy.env
    d = ledger.load()
    ledger.record_shown(d, J("1", req="R-77"), {"verdict": "IN", "fit": 4})
    ledger.record_shown(d, J("2", title="GenAI Engineer"))
    ledger.save(d)
    assert ledger.main(["skipped", "R-77"]) == 0
    assert ledger.main(["applied", "missing"]) == 1
    capsys.readouterr()
    ledger.main(["list"])
    out = capsys.readouterr().out
    assert "GenAI Engineer" in out and "R-77" not in out and "1 role(s)" in out
    ledger.main(["shown", "Acme Bank:1"])
    assert ledger.load()["roles"]["Acme Bank:1"]["status"] == "SHOWN"


def test_merge_keeps_repost_status():
    from detector.merge import _job
    assert _job({"first_seen": "a", "status": "pending"}, {"first_seen": "b", "status": "repost"})["status"] == "repost"
