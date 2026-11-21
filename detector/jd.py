"""Full job-description text for the screener. Fetchers and detail hooks set `Job.jd` where their API
carries it (Ashby, Amazon, Jibe, Atlassian, Workday, Oracle, Eightfold); this fills in the rest
(Greenhouse and SmartRecruiters detail APIs) and falls back to the JD page's visible text."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import httpx

from detector.sources import Job

MAX_CHARS = 12000  # enough for any real JD; keeps one screening call small


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    if "&lt;" in s and "<" not in s:  # Greenhouse sends escaped HTML
        s = html.unescape(s)
    p = _Text()
    p.feed(s)
    text = html.unescape("".join(p.out)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return text.strip()


def sections(*parts: tuple[str, str | None]) -> str:
    """Join (heading, html-or-text) parts, skipping empty ones."""
    out = []
    for head, body in parts:
        t = html_to_text(body)
        if t:
            out.append(f"{head}\n{t}" if head else t)
    return "\n\n".join(out)


def get(c: httpx.Client, job: Job) -> tuple[str, str]:
    """(text, source). Source says where it came from, so a page-text fallback is visible."""
    if job.jd:
        return job.jd[:MAX_CHARS], "ATS API"
    api = (job.check_urls or [""])[0]
    try:
        if "boards-api.greenhouse.io" in api:
            d = c.get(api).raise_for_status().json()
            return html_to_text(d.get("content"))[:MAX_CHARS], "Greenhouse API"
        if "api.smartrecruiters.com" in api:
            s = (c.get(api).raise_for_status().json().get("jobAd") or {}).get("sections") or {}
            text = sections(*[(v.get("title") or "", v.get("text")) for v in s.values() if isinstance(v, dict)])
            return text[:MAX_CHARS], "SmartRecruiters API"
        r = c.get(job.url)
        r.raise_for_status()
        return html_to_text(r.text)[:MAX_CHARS], "JD page text"
    except httpx.HTTPError as e:
        return "", f"unknown (JD fetch failed: {type(e).__name__})"
