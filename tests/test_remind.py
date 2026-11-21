"""Follow-through: closing reminders, follow-up nudges, Sunday lists. Time and HTTP are faked."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from detector import ledger, remind, screen

IST = remind.IST
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=IST)  # a Monday, 11:00 IST
SUNDAY_EVE = datetime(2026, 11, 29, 20, 30, tzinfo=IST)


def iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "private").mkdir()
    monkeypatch.setattr(screen, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(screen, "VERDICTS_FILE", tmp_path / "private" / "verdicts.json")
    pushed = []
    monkeypatch.setattr(remind.notify, "push", lambda title, body, **k: pushed.append((title, body)) or True)
    return pushed


def role(status="SHOWN", **kw):
    e = {"board": "JPMC", "id": "1", "title": "ML Engineer", "url": "https://jd/1", "apply_url": "https://apply/1",
         "status": status, "shown_at": iso(NOW - timedelta(days=3))}
    e.update(kw)
    return e


def save(**roles):
    d = ledger.load()
    d["roles"] = {f"JPMC:{k}": e for k, e in roles.items()}
    ledger.save(d)


def client(status=200, body=None):
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json=body if body is not None else {"items": [{"Id": 1}]})))


@pytest.mark.parametrize("s,expect", [
    ("2026-11-25", datetime(2026, 11, 25, 23, 59, tzinfo=IST)),
    ("2026-11-25T05:00:00+00:00", datetime(2026, 11, 25, 5, tzinfo=timezone.utc)),
    ("2026-12-30T18:30:00+0000", datetime(2026, 12, 30, 18, 30, tzinfo=timezone.utc)),
    ("garbage", None), (None, None),
])
def test_when_parses_stated_dates(s, expect):
    assert remind.when(s) == expect


def test_closing_reminder_once_for_shown_role_only(env):
    pushed = env
    close = iso(NOW + timedelta(days=1, hours=5))
    save(**{"1": role(closes_at=close, check_url="https://api/1", verdict="IN", fit=4),
            "2": role("APPLIED", closes_at=close, marked_at=iso(NOW)),
            "3": role(closes_at=iso(NOW + timedelta(days=6)))})
    remind.run(NOW, client())
    assert [t for t, _ in pushed] == ["Closing soon: JPMC: ML Engineer"]
    assert "in about 29 h" in pushed[0][1] and "IN fit 4/5" in pushed[0][1] and "Could not confirm" not in pushed[0][1]
    remind.run(NOW + timedelta(hours=1), client())
    assert len(pushed) == 1


def test_closed_early_gets_no_reminder_and_drops_from_weekly(env):
    pushed = env
    save(**{"1": role(closes_at=iso(NOW + timedelta(days=1)), check_url="https://api/1")})
    remind.run(NOW, client(body={"items": []}))  # Oracle: no open posting
    assert pushed == [] and ledger.load()["roles"]["JPMC:1"]["closed_at"]
    assert remind.unresolved(ledger.load(), NOW) == []


def test_unconfirmed_still_reminds_and_says_so(env):
    pushed = env
    save(**{"1": role(closes_at=iso(NOW + timedelta(days=1)), check_url="https://api/1")})
    remind.run(NOW, client(status=503))
    assert "Could not confirm it is still open: detail API HTTP 503" in pushed[0][1]


def test_quiet_hours_hold_reminders_unless_urgent(env):
    pushed = env
    night = datetime(2026, 11, 23, 23, 30, tzinfo=IST)
    save(**{"1": role(closes_at=iso(night + timedelta(days=1))),
            "2": role(id="2", title="GenAI Engineer", closes_at=iso(night + timedelta(hours=6)))})
    remind.run(night, client())
    assert [t for t, _ in pushed] == ["Closing soon: JPMC: GenAI Engineer"]


def test_followup_once_after_five_quiet_days(env):
    pushed = env
    save(**{"1": role("APPLIED", marked_at=iso(NOW - timedelta(days=5, hours=1))),
            "2": role("APPLIED", marked_at=iso(NOW - timedelta(days=2))),
            "3": role("SKIPPED", marked_at=iso(NOW - timedelta(days=9)))})
    remind.run(NOW, client())
    remind.run(NOW + timedelta(hours=1), client())
    assert [t for t, _ in pushed] == ["Applied 5+ days ago: JPMC: ML Engineer"]


def test_sunday_lists_once(env, tmp_path):
    pushed = env
    save(**{"1": role(verdict="IN", fit=4, shown_at=iso(SUNDAY_EVE - timedelta(days=2))),
            "2": role(title="Old", shown_at=iso(SUNDAY_EVE - timedelta(days=40))),
            "3": role("APPLIED", title="Done", marked_at=iso(SUNDAY_EVE))})
    at = (SUNDAY_EVE - timedelta(days=1)).isoformat(timespec="minutes")
    verdicts = {"a": {"skills": ["PyTorch", "RAG"], "screened_at": at},
                "b": {"skills": ["pytorch", "Kubernetes"], "screened_at": at},
                "c": {"skills": ["Fortran"], "screened_at": (SUNDAY_EVE - timedelta(days=10)).isoformat()},
                "d": {"verdict": "IN"}}  # screened before skills were collected
    (tmp_path / "private" / "verdicts.json").write_text(json.dumps(verdicts))
    (tmp_path / "private" / "resume.md").write_text("PyTorch, LangGraph, RAG pipelines")
    remind.run(SUNDAY_EVE - timedelta(hours=2), client())  # Sunday 18:30: too early
    assert pushed == []
    remind.run(SUNDAY_EVE, client())
    remind.run(SUNDAY_EVE + timedelta(minutes=30), client())
    (t1, b1), (t2, b2) = pushed
    assert t1 == "jobwatch: 1 role with no Applied/Skip" and "ML Engineer · IN fit 4" in b1 and "Old" not in b1
    assert t2 == "jobwatch: skills asked in 2 JDs this week"
    assert b2.splitlines()[0] == "PyTorch: 2" and "Kubernetes: 1 (not on resume)" in b2 and "Fortran" not in b2


def test_nothing_without_ledger(env):
    assert remind.run(NOW, client()) == [] and env == []


def test_dry_run_records_nothing(env, monkeypatch):
    monkeypatch.setattr(remind.notify, "push", lambda *a, **k: k.get("dry_run"))
    save(**{"1": role(closes_at=iso(NOW + timedelta(days=1)))})
    assert remind.run(NOW, client(), dry_run=True) == ["Closing soon: JPMC: ML Engineer"]
    assert "reminded_at" not in ledger.load()["roles"]["JPMC:1"]


def test_screener_reply_skills_parsed_and_capped():
    v = screen.parse(json.dumps({"verdict": "IN", "fit": 3, "skills": [" PyTorch ", ""] + [f"s{i}" for i in range(20)]}))
    assert v["skills"][0] == "PyTorch" and len(v["skills"]) == 12
    assert screen.parse('{"verdict": "OUT"}')["skills"] == []
