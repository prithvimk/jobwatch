"""Laptop runner: pull state -> detector pass -> commit state -> push -> heartbeat.

Run every 5 min by Windows Task Scheduler (scripts/install_task.ps1) through the venv's pythonw.exe,
so no console window appears. Output goes to private/logs/sync.log.

The laptop is the primary detector. GitHub Actions is the fallback: when the laptop has run in the
last 15 min it records that in the repo variable JOBWATCH_LOCAL_AT, and the scheduled workflow skips.
With no `origin` remote (repo not published yet) it runs and commits locally only.

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

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "private" / "logs" / "sync.log"
STATE_FILES = ["state/seen.json", "state/health.json"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       creationflags=NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def register_merge_driver() -> None:
    """Local git config (not committed): merge state by meaning, using this venv's python."""
    py = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
    driver = f'"{py.as_posix()}" -m detector.merge %O %A %B'
    if git("config", "merge.jobwatch-state.driver").stdout.strip() != driver:
        git("config", "merge.jobwatch-state.name", "union of jobwatch state", check=True)
        git("config", "merge.jobwatch-state.driver", driver, check=True)


def pull(branch: str) -> None:
    r = git("pull", "--rebase", "--autostash", "-q", "origin", branch)
    if r.returncode:
        git("rebase", "--abort")
        print(f"pull failed, running on local state: {r.stderr.strip()[:300]}")


def commit_state() -> bool:
    git("add", *STATE_FILES)
    if git("diff", "--cached", "--quiet", "--", *STATE_FILES).returncode == 0:
        return False
    # Commit only the state files, never anything else the user has staged.
    git("commit", "-q", "-m", f"state: {datetime.now().astimezone():%Y-%m-%dT%H:%M%z} (laptop)",
        "--", *STATE_FILES, check=True)
    return True


def push(branch: str) -> None:
    for _ in range(3):
        if git("push", "-q", "origin", branch).returncode == 0:
            return
        pull(branch)
    print("push failed 3 times; state stays committed locally and goes up next pass")


def heartbeat() -> None:
    r = subprocess.run(["gh", "variable", "set", "JOBWATCH_LOCAL_AT", "--body", str(int(time.time()))],
                       cwd=ROOT, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        print(f"heartbeat failed (Actions will also run): {r.stderr.strip()[:200]}")


def sync() -> int:
    print(f"--- {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}")
    branch = git("branch", "--show-current").stdout.strip()
    on_main = branch in ("main", "master")
    remote = on_main and bool(git("remote", "get-url", "origin").stdout.strip())
    if remote:
        register_merge_driver()
        pull(branch)
    detector.load_env()
    rc = detector.run(None, dry_run=False)
    if not on_main:
        print(f"on branch {branch!r}: state written but not committed")
        return rc
    if commit_state() and remote:
        push(branch)
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
