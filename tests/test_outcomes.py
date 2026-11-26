"""Outcomes: JD copies, closure checks, inbox-report import and the report."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from detector import ledger, outcomes, sources, state
from detector import run as runmod
from detector.sources import Job

NOW = datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc)


def entry(board, jid, title, req=None, status="APPLIED", marked="2026-11-24T06:00:00+00:00", **kw):
    e = {"board": board, "id": jid, "title": title, "status": status, "url": f"https://x/{jid}",
         "apply_url": f"https://x/{jid}/apply", "shown_at": "2026-11-24T05:50:00+00:00", "marked_at": marked}
    if req:
        e["req"] = req
    e.update(kw)
    return e


@pytest.fixture
def d():
    roles = {
        "JPMC:210811093": entry("JPMC", "210811093", "Applied AIML Associate Senior", "210811093", verdict="IN", fit=4,
                                posted_at="2026-11-24"),
        "JPMC:210808827": entry("JPMC", "210808827", "Applied AI & ML - Associate", "210808827", verdict="STRETCH", fit=3),
        "Amazon:10606600": entry("Amazon", "10606600", "Applied Scientist I, Worldwide Returns", "10606600",
                                 verdict="IN", fit=4, coverage=0.92),
        "Visa:REF1": entry("Visa", "REF1", "Sr. Software Engineer (Gen AI)", "REF1"),
        "Okta:9": entry("Okta", "9", "ML Engineer", status="SKIPPED"),
    }
    return {"version": 1, "roles": roles, "reply_since": ""}


# ---------------------------------------------------------------- import

def test_parse_csv_with_preamble_and_quotes():
    text = ('Here you go:\n```csv\ndate,company,role_title,req_id,stage,evidence\n'
            '2026-11-28,"JPMorgan Chase & Co.",Applied AIML Associate Senior,,rejected,"Update on your application, JPMC"\n'
            '```\n')
    [row] = outcomes.parse_report(text)
    assert row == {"date": "2026-11-28", "company": "JPMorgan Chase & Co.", "title": "Applied AIML Associate Senior",
                   "req": "", "stage": "rejected", "note": "Update on your application, JPMC"}


def test_parse_markdown_table():
    text = ("| Date | Company | Role Title | Req ID | Stage | Evidence |\n|---|---|---|---|---|---|\n"
            "| 2026-11-27 | Amazon | Applied Scientist I | 10606600 | assessment | Online assessment invite |\n")
    [row] = outcomes.parse_report(text)
    assert row["req"] == "10606600" and row["stage"] == "assessment" and row["company"] == "Amazon"


@pytest.mark.parametrize("text,stage", [
    ("rejected", "rejected"), ("Recruiter screen", "recruiter_screen"), ("recruiter_screen", "recruiter_screen"),
    ("Online Assessment", "assessment"), ("HackerRank test", "assessment"), ("Interview scheduled", "interview"),
    ("Application received", "acknowledged"), ("we regret to inform", "rejected"), ("offer letter", "offer"),
    ("something else", None),
])
def test_stage_synonyms(text, stage):
    assert outcomes.stage_of(text) == stage


def test_match_by_requisition_id(d):
    k, how, _ = outcomes.match(d, {"company": "Amazon Development Centre", "req": "10606600", "title": ""})
    assert k == "Amazon:10606600" and how == "requisition id"


def test_match_by_company_alias_and_title(d):
    k, how, _ = outcomes.match(d, {"company": "JPMorgan Chase & Co.", "title": "Applied AIML Associate Senior"})
    assert k == "JPMC:210811093" and how == "company and title"


def test_two_roles_at_company_without_title_is_not_guessed(d):
    k, how, cands = outcomes.match(d, {"company": "J.P. Morgan", "title": ""})
    assert k is None and sorted(cands) == ["JPMC:210808827", "JPMC:210811093"]


def test_only_applied_role_at_company_matches_without_title(d):
    k, how, _ = outcomes.match(d, {"company": "Visa Inc.", "title": ""})
    assert k == "Visa:REF1" and "only applied role" in how


def test_skipped_roles_and_unknown_companies_never_match(d):
    assert outcomes.match(d, {"company": "Okta", "title": "ML Engineer"})[0] is None
    assert outcomes.match(d, {"company": "Acme", "title": "ML Engineer"})[0] is None


def test_import_dedupes_and_reports_unmatched(d):
    rows = outcomes.parse_report(
        "date,company,role_title,req_id,stage,evidence\n"
        "2026-11-27,Amazon,,10606600,assessment,OA\n"
        "2026-11-27,Amazon,,10606600,assessment,OA again\n"
        "2026-11-28,JPMorgan,,,rejected,x\n"
        "2026-11-28,Visa,,,maybe,x\n")
    res = outcomes.import_rows(d, rows)
    assert len(res["added"]) == 1 and len(res["known"]) == 1 and len(res["unmatched"]) == 1 and len(res["bad"]) == 1
    assert d["roles"]["Amazon:10606600"]["events"] == [{"stage": "assessment", "at": "2026-11-27", "source": "inbox report",
                                                        "note": "OA"}]


# ---------------------------------------------------------------- report

def test_report_counts(d):
    outcomes.add_event(d["roles"]["Amazon:10606600"], "acknowledged", "2026-11-24", "t")
    outcomes.add_event(d["roles"]["Amazon:10606600"], "assessment", "2026-11-27", "t")
    outcomes.add_event(d["roles"]["JPMC:210811093"], "rejected", "2026-11-30", "t")
    outcomes.add_event(d["roles"]["JPMC:210808827"], "acknowledged", "2026-11-24", "t")  # ack only, 26 days: no reply
    d["roles"]["Visa:REF1"]["marked_at"] = "2026-12-15T06:00:00+00:00"                    # 5 days: waiting
    text = outcomes.report(d, NOW)
    assert "Applied through jobwatch: 4" in text
    assert "Any reply beyond an acknowledgement: 2; progressed to an assessment or further: 1" in text
    assert "in process 1, rejected 1, withdrawn 0, no reply 1, waiting 1" in text
    assert "median 4.5 (n=2)" in text
    assert "| IN | 2 | 2 | 1 | 1 | 0 | 0 |" in text
    assert "| same day | 1 |" in text and "| ≥0.90 | 1 |" in text


def test_prompt_lists_only_applied_roles(d):
    p = outcomes.prompt_text(d)
    assert "date,company,role_title,req_id,stage,evidence" in p
    assert "Applied AIML Associate Senior" in p and "Okta" not in p and "from 2026-11-24 onward" in p


# ---------------------------------------------------------------- closures and JD copies

@pytest.fixture
def hooks(monkeypatch):
    calls, state_ = [], {"JPMC:210811093": "closed", "JPMC:210808827": "throttled"}

    def enrich(c, job):
        calls.append(job.id)
        s = state_.get(f"{job.board}:{job.id}")
        if s == "closed":
            raise RuntimeError("detail API has no such open posting")
        if s == "throttled":
            raise RuntimeError("HTTP 429 from https://x")
        job.jd = f"JD for {job.title}"

    monkeypatch.setattr(sources, "detail_hooks", lambda kind: (enrich, lambda c, j: (True, "open")) if kind == "oracle" else None)
    return calls


KINDS = {"JPMC": "oracle", "Amazon": "amazon", "Visa": "workday"}


def test_closure_checks_record_closed_open_and_unconfirmed(d, hooks, monkeypatch):
    from detector import remind
    monkeypatch.setattr(remind, "still_open", lambda c, e: (True, "JD page loads"))
    n = outcomes.check_closed(None, d, KINDS, NOW)
    r = d["roles"]
    assert n == 1 and r["JPMC:210811093"]["closed_at"] == NOW.isoformat(timespec="seconds")
    assert "no such open posting" in r["JPMC:210811093"]["closed_why"]
    assert "closed_at" not in r["JPMC:210808827"] and "429" in r["JPMC:210808827"]["open_check_error"]
    assert r["Amazon:10606600"]["last_open_at"] and "open_checked_at" not in r["Okta:9"]
    hooks.clear()
    outcomes.check_closed(None, d, KINDS, NOW + timedelta(hours=2))   # checked under 20 h ago: skipped
    assert hooks == []
    outcomes.check_closed(None, d, KINDS, NOW + timedelta(hours=21))  # closed roles are not checked again
    assert hooks == ["210808827"]


def test_capture_jds_saves_and_gives_up_after_tries(d, hooks, monkeypatch):
    from detector import jd
    monkeypatch.setattr(jd, "get", lambda c, job: (job.jd, "ATS API") if job.jd else ("", "unknown (JD fetch failed: X)"))
    del d["roles"]["JPMC:210811093"]  # closed-on-fetch case is covered by the throttled one below
    for _ in range(4):
        outcomes.capture_jds(None, d, KINDS)
    r = d["roles"]
    assert r["Amazon:10606600"].get("jd_file") is None and r["Amazon:10606600"]["jd_tries"] == 3  # no hook, no jd
    assert r["JPMC:210808827"]["jd_tries"] == 3 and "429" in r["JPMC:210808827"]["jd_error"]
    d["roles"]["JPMC:210808827"].update(jd_tries=0)
    hooks_state = {"JPMC:210808827": None}
    monkeypatch.setattr(sources, "detail_hooks", lambda kind: ((lambda c, j: setattr(j, "jd", "Full JD text")),
                                                              lambda c, j: (True, "")) if kind == "oracle" else None)
    outcomes.capture_jds(None, d, KINDS)
    e = r["JPMC:210808827"]
    path = ledger.PRIVATE / "jds" / e["jd_file"]
    text = path.read_text(encoding="utf-8")
    assert e["jd_file"] == "JPMC-210808827.md" and "Full JD text" in text and "requisition: 210808827" in text
    assert "jd_tries" not in e and "jd_error" not in e


def test_alert_saves_jd_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    (tmp_path / "private").mkdir()
    monkeypatch.setattr(runmod, "load_boards", lambda names: [{"name": "Acme Bank", "ats": "fake", "locator": ["x"]}])
    monkeypatch.setattr(runmod, "liveness", lambda c, j: (True, "test"))
    monkeypatch.setattr(runmod.notify, "push", lambda title, body, **k: True)
    postings = []
    monkeypatch.setitem(runmod.FETCHERS, "fake", lambda c, b, **k: list(postings))
    runmod.run(None, dry_run=False)
    postings.append(Job("Acme Bank", "1", "ML Engineer", ["Pune, India"], "u1", "a1", None, jd="Build LLM apps."))
    runmod.run(None, dry_run=False)
    e = ledger.load()["roles"]["Acme Bank:1"]
    assert e["jd_file"] == "Acme-Bank-1.md" and e["jd_source"] == "ATS API"
    assert "Build LLM apps." in (ledger.PRIVATE / "jds" / "Acme-Bank-1.md").read_text(encoding="utf-8")
