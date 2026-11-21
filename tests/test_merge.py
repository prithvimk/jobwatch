import json
import subprocess
import sys
from pathlib import Path

from detector.merge import merge_health, merge_seen

ROOT = Path(__file__).resolve().parent.parent


def seen(**jobs):
    return {"version": 1, "boards": {"Acme": {"baseline_at": "2026-11-21T00:00:00+00:00", "jobs": jobs}}}


def test_union_and_status_precedence():
    a = seen(**{"1": {"first_seen": "T2", "status": "alerted", "alerted_at": "T3"},
                "2": {"first_seen": "T1", "status": "baseline"}})
    b = seen(**{"1": {"first_seen": "T1", "status": "pending", "tries": 1},
                "3": {"first_seen": "T4", "status": "filtered"}})
    jobs = merge_seen(a, b)["boards"]["Acme"]["jobs"]
    assert set(jobs) == {"1", "2", "3"}
    assert jobs["1"]["status"] == "alerted" and jobs["1"]["first_seen"] == "T1" and jobs["1"]["alerted_at"] == "T3"
    assert merge_seen(b, a)["boards"]["Acme"]["jobs"]["1"]["status"] == "alerted"  # symmetric


def test_board_only_on_one_side_and_earliest_baseline():
    a = seen()
    b = {"version": 1, "boards": {"Acme": {"baseline_at": "2026-11-20T00:00:00+00:00", "jobs": {}},
                                  "New": {"baseline_at": "X", "jobs": {"9": {"first_seen": "X", "status": "baseline"}}}}}
    m = merge_seen(a, b)["boards"]
    assert m["Acme"]["baseline_at"] == "2026-11-20T00:00:00+00:00" and "9" in m["New"]["jobs"]


def test_pending_keeps_max_tries():
    a = seen(**{"1": {"first_seen": "T", "status": "pending", "tries": 2}})
    b = seen(**{"1": {"first_seen": "T", "status": "pending", "tries": 1}})
    assert merge_seen(b, a)["boards"]["Acme"]["jobs"]["1"]["tries"] == 2


def test_health_keeps_failure():
    a = {"Acme": {"ok": True, "fails": 0}}
    b = {"Acme": {"ok": False, "fails": 2, "error": "x"}}
    assert merge_health(a, b)["Acme"]["fails"] == 2 and merge_health(b, a)["Acme"]["fails"] == 2


def test_git_rebase_uses_driver(tmp_path):
    """Two runners record different new IDs; a rebase merges them instead of conflicting."""
    def g(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)

    def write(repo, data):
        (repo / "state" / "seen.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

    origin, laptop, actions = tmp_path / "origin.git", tmp_path / "laptop", tmp_path / "actions"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(laptop)], check=True)
    for k, v in {"user.name": "t", "user.email": "t@t", "commit.gpgsign": "false"}.items():
        g(laptop, "config", k, v)
    (laptop / "state").mkdir()
    (laptop / ".gitattributes").write_text("state/*.json merge=jobwatch-state\n")
    write(laptop, seen())
    g(laptop, "add", "-A"); g(laptop, "commit", "-qm", "init"); g(laptop, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(origin), str(actions)], check=True)
    for repo in (laptop, actions):
        for k, v in {"user.name": "t", "user.email": "t@t", "commit.gpgsign": "false",
                     "merge.jobwatch-state.driver": f'"{Path(sys.executable).as_posix()}" -m detector.merge %O %A %B'}.items():
            g(repo, "config", k, v)

    write(actions, seen(**{"A": {"first_seen": "T1", "status": "alerted"}}))
    g(actions, "commit", "-qam", "actions"); g(actions, "push", "-q")
    write(laptop, seen(**{"A": {"first_seen": "T2", "status": "pending", "tries": 1},
                          "L": {"first_seen": "T2", "status": "filtered"}}))
    g(laptop, "commit", "-qam", "laptop")

    env_cwd = dict(cwd=ROOT)  # driver runs `-m detector.merge`; make the package importable
    r = subprocess.run(["git", "-C", str(laptop), "pull", "--rebase", "-q"], capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(ROOT)}, **env_cwd)
    assert r.returncode == 0, r.stderr
    jobs = json.loads((laptop / "state" / "seen.json").read_text())["boards"]["Acme"]["jobs"]
    assert jobs["A"]["status"] == "alerted" and jobs["A"]["first_seen"] == "T1" and "L" in jobs
