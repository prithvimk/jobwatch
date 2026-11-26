"""Status page: built from the ledger, verdicts, state and digest. Files live in tmp; time is fixed."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from detector import ledger, screen, state, status

NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)


def iso(t):
    return t.isoformat(timespec="seconds")


@pytest.fixture
def env(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    (priv / "kits").mkdir(parents=True)
    monkeypatch.setattr(screen, "PRIVATE", priv)
    monkeypatch.setattr(screen, "KITS", priv / "kits")
    monkeypatch.setattr(screen, "VERDICTS_FILE", priv / "verdicts.json")
    monkeypatch.setattr(screen, "DIGEST", priv / "digest.json")
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(status, "LOG", priv / "logs" / "sync.log")
    monkeypatch.setattr(status, "OUT", priv / "status.html")
    monkeypatch.setattr(status, "ROOT", tmp_path)
    monkeypatch.setattr(screen, "available", lambda: (True, "ok"))
    (tmp_path / "boards.yaml").write_text(
        "boards:\n- {name: JPMC, ats: oracle}\n- {name: Sarvam, ats: ashby}\n- {name: Google, ats: custom, platform: google-careers}\n",
        encoding="utf-8")
    return tmp_path


def seed(env):
    d = ledger.load()
    base = {"url": "https://jd/x", "apply_url": "https://apply/x", "shown_at": iso(NOW - timedelta(days=1))}
    d["roles"] = {
        "JPMC:1": {**base, "board": "JPMC", "id": "1", "status": "SHOWN", "title": "ML <Engineer>", "verdict": "STRETCH",
                   "fit": 3, "closes_at": iso(NOW + timedelta(days=1)), "req": "REQ1"},
        "JPMC:2": {**base, "board": "JPMC", "id": "2", "status": "SHOWN", "title": "Applied Scientist", "verdict": "IN", "fit": 5},
        "JPMC:3": {**base, "board": "JPMC", "id": "3", "status": "APPLIED", "title": "Data Scientist",
                   "marked_at": iso(NOW - timedelta(days=2))},
        "JPMC:4": {**base, "board": "JPMC", "id": "4", "status": "SKIPPED", "title": "Old Role",
                   "marked_at": iso(NOW - timedelta(days=40))},
    }
    ledger.save(d)
    screen.save_verdicts({"JPMC:2": {"verdict": "IN", "fit": 5, "one_line": "Strong RAG match", "gaps": ["Spark"],
                                     "location": "Mumbai", "experience_asked": "2+ years"}})
    (screen.KITS / "2026-11-22-JPMC-2.md").write_text("kit", encoding="utf-8")
    state.save_seen({"version": 1, "boards": {"JPMC": {"baseline_at": iso(NOW - timedelta(days=5)), "jobs": {
        "0": {"first_seen": iso(NOW - timedelta(days=5)), "status": "baseline"},
        "1": {"first_seen": iso(NOW - timedelta(days=1)), "status": "alerted"},
        "9": {"first_seen": iso(NOW - timedelta(days=2)), "status": "pending"},
        "8": {"first_seen": iso(NOW - timedelta(days=20)), "status": "filtered"}}}}})
    state.save_health({"JPMC": {"ok": True, "fails": 0},
                       "Sarvam": {"ok": False, "fails": 4, "error": "ConnectTimeout: boom", "failing_since": iso(NOW)}})


def test_roles_best_fit_first_escaped_with_verdict_kit_and_closing(env):
    seed(env)
    html = status.build(NOW)
    todo = html.split("<h2>To act on <span class='count'>(2)</span></h2>")[1].split("<h2>")[0]
    assert todo.index("Applied Scientist") < todo.index("ML &lt;Engineer&gt;")  # fit 5 before fit 3
    assert "<Engineer>" not in html
    assert "Strong RAG match" in todo and "Spark" in todo and "Mumbai" in todo
    assert "2026-11-22-JPMC-2.md" in todo  # kit link
    row1 = todo.split('data-key="JPMC:1"')[1].split("</tr>")[0]
    assert 'class="nowrap urgent"' in row1 and "REQ1" in row1  # closes within 2 days
    resolved = html.split("<h2>Resolved, last 30 days <span class='count'>(1: 1 applied)</span></h2>")[1].split("<h2>")[0]
    assert "Data Scientist" in resolved and "Old Role" not in resolved


def test_buttons_use_reply_topic(env, monkeypatch):
    seed(env)
    assert 'REPLY=""' in status.build(NOW)  # no topic: the buttons show the CLI command instead
    monkeypatch.setenv("NTFY_TOPIC", "secret-topic")
    assert 'REPLY="https://ntfy.sh/secret-topic-reply"' in status.build(NOW)


def test_boards_failing_first_unknown_visible_and_counts(env):
    seed(env)
    boards = status.build(NOW).split("<h2>Boards <span class='count'>(")[1]
    assert boards.startswith("2 watched · 1 failing · 1 not watched)")
    assert boards.index("Sarvam") < boards.index("Google") < boards.index("JPMC")
    assert "ConnectTimeout: boom" in boards and "not watched" in boards
    jpmc = boards.split("<td>JPMC</td>")[1].split("</tr>")[0]
    cells = [c.split(">", 1)[1] for c in jpmc.split("</td>")[:-1]]
    assert cells[3:6] == ["4", "2", "1"]  # IDs, new in 7 days (alerted + pending), pending
    activity = status.build(NOW).split("<h2>Last 7 days <span class='count'>(2 new)</span></h2>")[1].split("<h2>")[0]
    assert "<b>1</b> alerted" in activity and "<b>1</b> pending" in activity and "filtered" not in activity


def test_problems_only_from_the_last_pass_and_digest(env):
    seed(env)
    status.LOG.parent.mkdir(parents=True)
    status.LOG.write_text("--- old\nJPMC UNKNOWN (fetch failed: old)\n--- new\nSarvam UNKNOWN (fetch failed: x)\nok line\n",
                          encoding="utf-8")
    screen.DIGEST.write_text(json.dumps({"pending": [{"board": "Amazon", "title": "Risk Lead", "url": "https://a/1",
                                                      "reason": "7+ years"}], "last_sent": ""}), encoding="utf-8")
    html = status.build(NOW)
    assert "Sarvam UNKNOWN" in html and "fetch failed: old" not in html and "ok line" not in html
    assert "Risk Lead" in html and "7+ years" in html
    assert "Runner <span class='count'>(1 problem this pass)</span>" in html
    assert "Screened out, queued for tonight's digest <span class='count'>(1)</span>" in html


def test_write_with_no_private_data(env):
    p = status.write(NOW)
    html = p.read_text(encoding="utf-8")
    assert "Nothing waiting on you." in html and "no state yet" in html
