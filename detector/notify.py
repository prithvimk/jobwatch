"""ntfy push. Topic comes from NTFY_TOPIC (a secret: anyone who knows it can read the alerts).
Without NTFY_TOPIC, or with dry_run, messages are printed instead of sent."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

IST = timezone(timedelta(hours=5, minutes=30))


def ist(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).strftime("%d %b %H:%M IST")


def push(title: str, message: str, click: str | None = None, actions: list[dict] | None = None,
         tags: list[str] | None = None, priority: int = 3, dry_run: bool = False) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if dry_run or not topic:
        print(f"[push{' dry-run' if dry_run else ' skipped: NTFY_TOPIC unset'}] {title}\n    " + message.replace("\n", "\n    ")
              + (f"\n    -> {click}" if click else ""))
        return dry_run  # unset topic is not success: the posting stays pending and is retried
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = {"topic": topic, "title": title, "message": message, "priority": priority}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions
    if tags:
        body["tags"] = tags
    headers = {"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}"} if os.environ.get("NTFY_TOKEN") else {}
    try:
        r = httpx.post(server, json=body, headers=headers, timeout=15)
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        print(f"[push failed] {title}: {e}")
        return False
