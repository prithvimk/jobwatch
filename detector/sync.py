"""Laptop runner: pull state -> read phone ledger taps -> detector pass -> digest and reminders -> status page
-> commit state -> push state -> heartbeat.

Run every 5 min by Windows Task Scheduler (scripts/install_task.ps1) through the venv's pythonw.exe,
so no console window appears. Output goes to private/logs/sync.log.

State lives on its own branch, `state`, checked out as a git worktree at state/ (gitignored on main).
This runner commits and pushes only that branch: it never pulls, commits or pushes `main`, so code
goes to GitHub only when the user pushes it. The worktree is created on the first pass if missing.

The laptop is the primary detector. GitHub Actions is the fallback: when the laptop has run in the
last 15 min it records that in the repo variable JOBWATCH_LOCAL_AT, and the scheduled workflow skips.
With no `origin` remote it runs and commits locally only.

  uv run python -m detector.sync            # one pass, logs to stdout
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from detector import run as detector
from detector import ledger, notify, remind, screen, status

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "private" / "logs" / "sync.log"
STATE = ROOT / "state"          # worktree of the `state` branch
STATE_BRANCH = "state"
STATE_FILES = ["seen.json", "health.json"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class StateError(RuntimeError):
    pass


def git(*args: str, check: bool = False, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       creationflags=NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def sgit(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """git inside the state worktree."""
    return git(*args, check=check, cwd=STATE)


def register_merge_driver() -> None:
    """Local git config (not committed, shared by the worktrees): merge state by meaning.
    merge.py is run as a script, since git runs the driver from the state worktree."""
    py = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
    driver = f'"{py.as_posix()}" "{(ROOT / "detector" / "merge.py").as_posix()}" %O %A %B'
    if git("config", "merge.jobwatch-state.driver").stdout.strip() != driver:
        git("config", "merge.jobwatch-state.name", "union of jobwatch state", check=True)
        git("config", "merge.jobwatch-state.driver", driver, check=True)


def ensure_state_worktree(remote: bool) -> None:
    """state/ must be the `state` branch's worktree. Running without it would baseline every board
    from empty state, so a missing or foreign state/ stops the pass instead."""
    if (STATE / ".git").exists():
        if sgit("branch", "--show-current").stdout.strip() != STATE_BRANCH:
            raise StateError(f"{STATE} is not on branch {STATE_BRANCH!r}")
        return
    if STATE.exists() and any(STATE.iterdir()):
        raise StateError(f"{STATE} exists but is not the {STATE_BRANCH!r} worktree; move it aside")
    if git("rev-parse", "--verify", "-q", f"refs/heads/{STATE_BRANCH}").returncode:
        if not remote or git("fetch", "-q", "origin", f"{STATE_BRANCH}:{STATE_BRANCH}").returncode:
            raise StateError(f"no {STATE_BRANCH!r} branch locally or on origin")
    git("worktree", "add", "-q", str(STATE), STATE_BRANCH, check=True)
    print(f"created the {STATE_BRANCH!r} worktree at {STATE}")


def pull() -> None:
    r = sgit("pull", "--rebase", "--autostash", "-q", "origin", STATE_BRANCH)
    if r.returncode:
        sgit("rebase", "--abort")
        print(f"state pull failed, running on local state: {r.stderr.strip()[:300]}")


def commit_state() -> bool:
    sgit("add", "--", *STATE_FILES)
    if sgit("diff", "--cached", "--quiet", "--", *STATE_FILES).returncode == 0:
        return False
    sgit("commit", "-q", "-m", f"state: {datetime.now().astimezone():%Y-%m-%dT%H:%M%z} (laptop)",
         "--", *STATE_FILES, check=True)
    return True


def unpushed() -> bool:
    """Local state commits not on origin yet (or the branch is not on origin at all)."""
    r = sgit("rev-list", "--count", f"origin/{STATE_BRANCH}..HEAD")
    return r.returncode != 0 or r.stdout.strip() != "0"


def push() -> None:
    for _ in range(3):
        if sgit("push", "-q", "origin", f"HEAD:{STATE_BRANCH}").returncode == 0:
            return
        pull()
    print("state push failed 3 times; it stays committed locally and goes up next pass")


def heartbeat() -> None:
    r = subprocess.run(["gh", "variable", "set", "JOBWATCH_LOCAL_AT", "--body", str(int(time.time()))],
                       cwd=ROOT, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        print(f"heartbeat failed (Actions will also run): {r.stderr.strip()[:200]}")


def send_digest() -> None:
    """Once a day after 20:00 IST: the roles the screener judged OUT, one line each with the reason."""
    due, d = screen.digest_due()
    if not due:
        return
    n = len(d["pending"])
    if notify.push(f"jobwatch: {n} role{'s' if n != 1 else ''} screened out today", screen.digest_text(d),
                   tags=["clipboard"], priority=2):
        screen.mark_digest_sent(d)
        print(f"digest sent ({n} roles)")


def sync() -> int:
    print(f"--- {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}")
    remote = bool(git("remote", "get-url", "origin").stdout.strip())
    register_merge_driver()
    try:
        ensure_state_worktree(remote)
    except (StateError, RuntimeError) as e:
        print(f"state unavailable, pass skipped: {e}")
        return 1
    if remote:
        pull()
    detector.load_env()
    ledger.pull()  # phone taps (Applied / Skip) before the pass, so a skipped role's repost is suppressed
    ok, why = screen.available()
    print(f"screening: {'on' if ok else 'off (' + why + ')'}")
    rc = detector.run(None, dry_run=False, screen_roles=ok)
    send_digest()
    try:
        remind.run()  # closing-date reminders, 5-day follow-ups, Sunday lists
    except Exception as e:  # a reminder bug must not stop state from being committed
        print(f"reminders failed: {type(e).__name__}: {e}")
    try:
        status.write()  # private/status.html: ledger, board health, this pass's problems
    except Exception as e:
        print(f"status page failed: {type(e).__name__}: {e}")
    commit_state()
    if remote and unpushed():
        push()
    if remote:
        heartbeat()
    return rc


def main() -> int:
    if sys.stdout is None or "--log" in sys.argv:  # pythonw has no console
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    elif hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        return sync()
    except Exception as e:  # a crash must leave a trace in the log
        import traceback
        traceback.print_exc()
        print(f"sync failed: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
