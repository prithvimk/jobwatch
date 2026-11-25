"""Cheap title/location prefilter. Returns a reason string for every decision so sweeps can report it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from detector.sources import Job


@dataclass
class Prefilter:
    include: list[re.Pattern]
    exclude: list[re.Pattern]
    india: re.Pattern
    north: re.Pattern
    boards: dict
    # JD keyword pass for titles the include list misses: (pattern, weight) pairs, threshold, per-pass cap.
    jd_rules: list[tuple[re.Pattern, int, str]] = field(default_factory=list)
    jd_threshold: int = 0
    jd_max_per_pass: int = 0
    jd_title_exclude: re.Pattern | None = None  # functions never worth a JD read
    jd_quals: re.Pattern | None = None  # where the qualifications start; text before it (team blurbs) is not scored

    @classmethod
    def load(cls, path: str | Path = Path(__file__).with_name("config.yaml")) -> "Prefilter":
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        rx = lambda p: re.compile(p, re.I)
        jc = cfg.get("jd_check") or {}
        rules = [(rx(r["pattern"]), int(r["weight"]), r.get("name") or r["pattern"]) for r in jc.get("signals", [])]
        return cls([rx(p) for p in cfg["title_include"]], [rx(p) for p in cfg["title_exclude"]],
                   rx(cfg["india"]), rx(cfg["north"]), cfg.get("boards") or {},
                   rules, int(jc.get("threshold", 0)), int(jc.get("max_per_pass", 0)),
                   rx(jc["title_exclude"]) if jc.get("title_exclude") else None,
                   rx(jc["quals_heading"]) if jc.get("quals_heading") else None)

    def location_ok(self, loc: str) -> bool:
        """In India and not North India. A string naming a north city AND another Indian city is kept."""
        if not self.india.search(loc):
            return False
        if not self.north.search(loc):
            return True
        rest = re.sub(r"\bindian?\b", " ", self.north.sub(" ", loc), flags=re.I)
        return bool(self.india.search(rest))  # another (non-north) Indian city remains

    def title_hit(self, job: Job) -> bool:
        """The title matches an include pattern (before exclusions): a role worth naming when filtered out."""
        extra = [re.compile(p, re.I) for p in (self.boards.get(job.board) or {}).get("extra_include", [])]
        return any(p.search(job.title) for p in self.include + extra)

    def check(self, job: Job, title_ok: bool = False) -> tuple[bool, str]:
        """title_ok: the JD keyword pass already let this title in, so only exclusions and location apply."""
        title = job.title
        for p in self.exclude:
            if m := p.search(title):
                return False, f"title excluded ({m.group(0)!r})"
        if not title_ok and not self.title_hit(job):
            return False, "title not GenAI/ML"
        locs = [l for l in job.locations if l and l.strip()]
        if not locs:
            return True, "location unknown (kept)"
        ok = [l for l in locs if self.location_ok(l)]
        if not ok:
            north = [l for l in locs if self.india.search(l)]
            return False, f"location North India ({'; '.join(north)})" if north else f"location outside India ({'; '.join(locs)[:80]})"
        return True, f"ok ({ok[0]})"

    def jd_eligible(self, job: Job) -> bool:
        """The title matched no include pattern but nothing excludes the posting: its JD gets a keyword read."""
        if not self.jd_rules or self.title_hit(job):
            return False
        if self.jd_title_exclude and self.jd_title_exclude.search(job.title):
            return False
        return self.check(job, title_ok=True)[0]

    def jd_score(self, text: str) -> tuple[int, list[str]]:
        """Sum of the weights of the distinct signals the JD names (each counts once, however often it appears).
        Only the text from the first qualifications heading on is scored, so a company or team blurb that talks
        about GenAI does not lift an unrelated role; a JD with no such heading is scored whole."""
        text = text or ""
        if self.jd_quals and (m := self.jd_quals.search(text)):
            text = text[m.start():]
        hits = [(w, name) for p, w, name in self.jd_rules if p.search(text)]
        return sum(w for w, _ in hits), [name for w, name in sorted(hits, key=lambda h: -h[0])]
