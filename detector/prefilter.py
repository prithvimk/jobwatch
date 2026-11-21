"""Cheap title/location prefilter. Returns a reason string for every decision so sweeps can report it."""

from __future__ import annotations

import re
from dataclasses import dataclass
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

    @classmethod
    def load(cls, path: str | Path = Path(__file__).with_name("config.yaml")) -> "Prefilter":
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        rx = lambda p: re.compile(p, re.I)
        return cls([rx(p) for p in cfg["title_include"]], [rx(p) for p in cfg["title_exclude"]],
                   rx(cfg["india"]), rx(cfg["north"]), cfg.get("boards") or {})

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

    def check(self, job: Job) -> tuple[bool, str]:
        title = job.title
        for p in self.exclude:
            if m := p.search(title):
                return False, f"title excluded ({m.group(0)!r})"
        extra = [re.compile(p, re.I) for p in (self.boards.get(job.board) or {}).get("extra_include", [])]
        if not any(p.search(title) for p in self.include + extra):
            return False, "title not GenAI/ML"
        locs = [l for l in job.locations if l and l.strip()]
        if not locs:
            return True, "location unknown (kept)"
        ok = [l for l in locs if self.location_ok(l)]
        if not ok:
            north = [l for l in locs if self.india.search(l)]
            return False, f"location North India ({'; '.join(north)})" if north else f"location outside India ({'; '.join(locs)[:80]})"
        return True, f"ok ({ok[0]})"
