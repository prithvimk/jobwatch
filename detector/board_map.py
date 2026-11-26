"""M0: classify each tracked careers board by ATS and write boards.yaml.

For every seed entry:
  1. Use explicit `hints` if present.
  2. Otherwise fetch careers_url (following redirects) and fingerprint the final URL + HTML.
  3. Confirm the detected endpoint by calling its public API and recording total / India counts.
Anything unreachable or not confirmable is written as `unknown` with the reason. Nothing is guessed silently.

Usage: uv run python -m detector.board_map [--seed boards_seed.yaml] [--out boards.yaml] [--only NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# ATS types with public JSON and no per-company code.
EASY = {"greenhouse", "lever", "ashby", "smartrecruiters"}
# ATS types with a uniform-but-undocumented API: one generic adapter, per-company config.
ADAPTER = {"workday", "oracle"}

# Locators that identify a specific board. Order matters: first match wins.
LOCATORS: list[tuple[str, re.Pattern]] = [
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("workday", re.compile(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([a-z0-9-]+)/([A-Za-z0-9_-]+)")),
    ("oracle", re.compile(r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([A-Za-z0-9_]+)")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
]

# Platforms that mean "custom adapter" but are worth naming.
PLATFORMS: list[tuple[str, re.Pattern]] = [
    ("eightfold", re.compile(r"eightfold\.ai|/api/apply/v2/jobs|pcsx", re.I)),
    ("phenom", re.compile(r"phenompeople|phApp\.|\"phenom", re.I)),
    ("successfactors", re.compile(r"successfactors|career\d*\.sapsf|rmk-map", re.I)),
    ("icims", re.compile(r"icims\.com", re.I)),
    ("taleo", re.compile(r"taleo\.net", re.I)),
    ("avature", re.compile(r"avature\.net", re.I)),
    ("radancy", re.compile(r"talentbrew|tbcdn\.|radancy", re.I)),
    ("zwayam", re.compile(r"zwayam", re.I)),
    ("darwinbox", re.compile(r"darwinbox", re.I)),
    ("mynexthire", re.compile(r"mynexthire", re.I)),
    ("keka", re.compile(r"keka\.com", re.I)),
    ("freshteam", re.compile(r"freshteam", re.I)),
]

INDIA = re.compile(r"\bindia\b|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|gurugram|gurgaon|noida|delhi|kolkata|ahmedabad", re.I)


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=TIMEOUT, follow_redirects=True)


# --------------------------------------------------------------------------- probes
# Each probe returns a dict with `endpoint`, `total`, `india` (int or "unknown"), or raises.

def probe_greenhouse(c: httpx.Client, slug: str) -> dict:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = c.get(url); r.raise_for_status()
    jobs = r.json()["jobs"]
    from detector.sources import _gh_meta_locations
    locs = lambda j: " ".join([(j.get("location") or {}).get("name", "")] + _gh_meta_locations(j))
    return {"endpoint": url, "total": len(jobs), "india": sum(bool(INDIA.search(locs(j))) for j in jobs)}


def probe_lever(c: httpx.Client, slug: str) -> dict:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = c.get(url); r.raise_for_status()
    jobs = r.json()
    return {"endpoint": url, "total": len(jobs), "india": sum(bool(INDIA.search(str(j.get("categories", {}).get("location", "")) + " " + str(j.get("country", "")))) for j in jobs)}


def probe_ashby(c: httpx.Client, slug: str) -> dict:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = c.get(url); r.raise_for_status()
    jobs = r.json()["jobs"]
    def loc(j):
        parts = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        addr = (j.get("address") or {}).get("postalAddress") or {}
        parts.append(addr.get("addressCountry", ""))
        return " ".join(p for p in parts if p)
    return {"endpoint": url, "total": len(jobs), "india": sum(bool(INDIA.search(loc(j))) for j in jobs)}


def probe_smartrecruiters(c: httpx.Client, slug: str) -> dict:
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    r = c.get(base, params={"limit": 1}); r.raise_for_status()
    total = r.json()["totalFound"]
    if total == 0:
        raise ValueError("smartrecruiters board has 0 postings (slug likely wrong)")
    ri = c.get(base, params={"limit": 1, "country": "in"}); ri.raise_for_status()
    return {"endpoint": base, "total": total, "india": ri.json()["totalFound"]}


WORKDAY_LOCK = threading.Lock()  # Workday throttles per source IP across ALL tenants: serialize + pace.


def probe_workday(c: httpx.Client, host: str, tenant: str, site: str) -> dict:
    with WORKDAY_LOCK:
        try:
            return _probe_workday(c, host, tenant, site)
        finally:
            time.sleep(3)


def _probe_workday(c: httpx.Client, host: str, tenant: str, site: str) -> dict:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    r = c.post(url, json=body, headers={"Accept": "application/json"})
    if r.status_code == 429:
        # Workday's edge throttles Python's TLS fingerprint per IP (httpx and urllib both get 429)
        # while system curl passes. Fall back to curl; M1 must pick a durable fix (e.g. curl_cffi).
        data = curl_json(url, body)
        if data is None:
            r.raise_for_status()
    else:
        r.raise_for_status()
        data = r.json()
    india, facet = workday_india(data.get("facets") or [])
    out = {"endpoint": url, "total": data.get("total"), "india": india}
    if facet:
        out["india_facet"] = facet  # appliedFacets key + id the detector should filter on
    return out


def curl_json(url: str, body: dict) -> dict | None:
    import json as _json
    import shutil
    import subprocess
    if not shutil.which("curl"):
        return None
    p = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", "-X", "POST", "-H", "Content-Type: application/json",
                        "-H", "Accept: application/json", "--data-binary", "@-", url],
                       input=_json.dumps(body), capture_output=True, text=True, encoding="utf-8", timeout=30)
    payload, _, code = p.stdout.rpartition("\n")
    return _json.loads(payload) if code == "200" else None


def workday_india(facets: list, parent: str = "") -> tuple[int | str, dict | None]:
    """Find the country-level 'India' value in a Workday facet tree (facet names differ per tenant)."""
    for f in facets:
        param = f.get("facetParameter") or parent
        for v in f.get("values") or []:
            if (v.get("descriptor") or "").strip().lower() == "india" and "id" in v:
                return v.get("count", "unknown"), {"param": param, "id": v["id"]}
            if v.get("values"):
                found = workday_india([v], param)
                if found[1]:
                    return found
    return "unknown", None


def probe_oracle(c: httpx.Client, host: str, site: str) -> dict:
    url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    def q(extra: str = "") -> dict:
        r = c.get(url, params={"onlyData": "true", "expand": "requisitionList", "finder": f"findReqs;siteNumber={site},limit=1{extra}"})
        r.raise_for_status()
        return r.json()["items"][0]
    item = q(",facetsList=LOCATIONS")
    india = next((l["TotalCount"] for l in item.get("locationsFacet") or [] if l.get("Name") == "India"), "unknown")
    return {"endpoint": f"{url}?finder=findReqs;siteNumber={site}", "total": item["TotalJobsCount"], "india": india, "india_method": "LOCATIONS facet, country=India"}


# ---- custom platforms with stable (undocumented) JSON. Hint value is the base URL / domain.

def probe_jibe(c: httpx.Client, base: str) -> dict:
    """Jibe (iCIMS front end) career sites: <base>/api/jobs."""
    url = f"{base.rstrip('/')}/api/jobs"
    total = c.get(url, params={"limit": 1}).raise_for_status().json()["totalCount"]
    india = c.get(url, params={"limit": 1, "location": "India"}).raise_for_status().json()["totalCount"]
    return {"endpoint": url, "total": total, "india": india}


def probe_eightfold(c: httpx.Client, spec: str) -> dict:
    """Eightfold PCSX search. spec = '<host>|<domain>'."""
    host, domain = spec.split("|")
    url = f"https://{host}/api/pcsx/search"
    r = c.get(url, params={"domain": domain, "location": "India", "start": 0})
    if r.status_code == 200 and "json" in r.headers.get("content-type", "") and "data" in r.json():
        return {"endpoint": f"{url}?domain={domain}", "total": "unknown", "india": r.json()["data"]["count"]}
    # older tenants (HSBC): "PCSX is not enabled", the v2 API answers instead
    url = f"https://{host}/api/apply/v2/jobs"
    india = c.get(url, params={"domain": domain, "location": "India", "start": 0}).raise_for_status().json()["count"]
    return {"endpoint": f"{url}?domain={domain}", "total": "unknown", "india": india}


def probe_amazon(c: httpx.Client, _: str) -> dict:
    url = "https://www.amazon.jobs/en/search.json"
    total = c.get(url, params={"result_limit": 1}).raise_for_status().json()["hits"]
    total = f"{total}+" if total >= 10000 else total  # search.json caps hits at 10000
    india = c.get(url, params={"result_limit": 1, "country": "IND"}).raise_for_status().json()["hits"]
    return {"endpoint": url, "total": total, "india": india}


def probe_atlassian(c: httpx.Client, _: str) -> dict:
    url = "https://www.atlassian.com/endpoint/careers/listings"
    jobs = c.get(url).raise_for_status().json()
    return {"endpoint": url, "total": len(jobs), "india": sum(bool(INDIA.search(" ".join(j.get("locations") or []))) for j in jobs)}


def probe_beesite(c: httpx.Client, tenant: str) -> dict:
    """milch & zucker BeeSite (Deutsche Bank). One call returns everything; India counted locally."""
    import json as _json
    url = f"https://api-{tenant}.beesite.de/search/"
    q = {"LanguageCode": "en", "SearchParameters": {"FirstItem": 1, "CountItem": 5000}, "SearchCriteria": []}
    res = c.get(url, params={"data": _json.dumps(q)}).raise_for_status().json()["SearchResult"]
    india = sum(any(l.get("CountryCode") == "IN" for l in i["MatchedObjectDescriptor"].get("PositionLocation", []))
                for i in res["SearchResultItems"])
    return {"endpoint": url, "total": res["SearchResultCountAll"], "india": india}


def probe_boa(c: httpx.Client, _: str) -> dict:
    """Bank of America: one servlet call returns every posting with city/country/postedDate/years. India filtered locally."""
    url = "https://careers.bankofamerica.com/services/jobssearchservlet"
    d = c.get(url, params={"start": 0, "rows": 5000, "search": "getAllJobs"}).raise_for_status().json()
    return {"endpoint": url, "total": d["totalMatches"], "india": sum(j.get("country") == "India" for j in d["jobsList"])}


def probe_gr8people(c: httpx.Client, base: str) -> dict:
    """gr8 People career sites: GraphQL at <base>/graphql (see detector/gr8people.py). There is no country
    filter, and the site's location aggregation counts a multi-location posting once per place, so India
    is counted from the postings themselves."""
    from detector import gr8people
    url = f"{base.rstrip('/')}/graphql"
    india = gr8people.fetch(c, {"name": "probe", "endpoint": url})
    q = "query q { s: searchJobPostings(first: 1) { results { totalCount } } }"
    d = c.post(url, json={"query": q}).raise_for_status().json()
    return {"endpoint": url, "total": d["data"]["s"]["results"]["totalCount"], "india": len(india)}


def probe_radancy(c: httpx.Client, base: str) -> dict:
    """Radancy (TalentBrew) sites: <base>/search-jobs/results with the India country facet (see detector/radancy.py)."""
    from detector import radancy
    url = f"{base.rstrip('/')}/search-jobs/results"
    r = c.get(url, params=radancy.params(1), headers={"X-Requested-With": "XMLHttpRequest"}).raise_for_status()
    india = radancy.TOTAL.search(r.json()["results"])
    return {"endpoint": url, "total": "unknown", "india": int(india.group(1)) if india else "unknown"}


def probe_rippling(c: httpx.Client, slug: str) -> dict:
    """Rippling's own ATS: one call lists every posting worldwide (see detector/rippling.py)."""
    from detector import rippling
    url = f"https://ats.rippling.com/api/v2/board/{slug}/jobs"
    total = c.get(url, params={"page": 0, "pageSize": 1}).raise_for_status().json()["totalItems"]
    return {"endpoint": url, "total": total, "india": len(rippling.fetch(c, {"name": "probe", "endpoint": url}))}


CUSTOM_PROBES = {"jibe": probe_jibe, "eightfold": probe_eightfold, "amazon": probe_amazon,
                 "atlassian": probe_atlassian, "beesite": probe_beesite, "boa": probe_boa,
                 "gr8people": probe_gr8people, "radancy": probe_radancy, "rippling": probe_rippling}


class RateLimited(Exception):
    pass


def confirm(c: httpx.Client, ats: str, groups: tuple) -> dict:
    if ats == "greenhouse":
        return probe_greenhouse(c, groups[0])
    if ats == "lever":
        return probe_lever(c, groups[0])
    if ats == "ashby":
        return probe_ashby(c, groups[0])
    if ats == "smartrecruiters":
        return probe_smartrecruiters(c, groups[0])
    if ats == "workday":
        if groups[0].startswith("wd"):  # myworkdaysite.com form: (wdN, tenant, site)
            wd, tenant, site = groups
            return probe_workday(c, f"{wd}.myworkdaysite.com", tenant, site)
        tenant, wd, site = groups
        return probe_workday(c, f"{tenant}.{wd}.myworkdayjobs.com", tenant, site)
    if ats == "oracle":
        return probe_oracle(c, groups[0], groups[1])
    raise ValueError(ats)


# --------------------------------------------------------------------------- classify

def locate(text: str) -> list[tuple[str, tuple]]:
    """All distinct ATS locators found in text, in LOCATORS priority order."""
    seen, out = set(), []
    for ats, pat in LOCATORS:
        for m in pat.finditer(text):
            key = (ats, m.groups())
            if key not in seen and m.group(1).lower() not in {"embed", "v1", "js"}:
                seen.add(key); out.append(key)
    return out


def hint_locators(hints: dict) -> list[tuple[str, tuple]]:
    out = []
    for ats, val in (hints or {}).items():
        if ats in EASY:
            out.append((ats, (val,)))
        else:
            out.extend(l for l in locate(val) if l[0] == ats)
    return out


def classify(entry: dict) -> dict:
    name, url = entry["name"], entry["careers_url"]
    res: dict = {"name": name, "careers_url": url}
    if entry.get("confirm"):
        res["needs_confirmation"] = entry["confirm"]
    if entry.get("note"):
        res["note"] = entry["note"]
    evidence: list[str] = []
    with client() as c:
        candidates = hint_locators(entry.get("hints"))
        if candidates:
            evidence.append("from seed hints")
        html, final = "", url
        try:
            r = c.get(url)
            final, html = str(r.url), r.text
            evidence.append(f"GET {url} -> {r.status_code} {final}")
            if r.status_code >= 400:
                html = ""
        except Exception as e:  # unreachable: keep going with hints only
            evidence.append(f"GET {url} failed: {type(e).__name__}: {e}")
        # An explicit custom-JSON hint beats whatever ATS the landing page happens to reference.
        if not any(k in CUSTOM_PROBES for k in (entry.get("hints") or {})):
            candidates += [l for l in locate(final + "\n" + html) if l not in candidates]

        errors, rate_limited = [], None
        for ats, groups in candidates:
            try:
                info = confirm(c, ats, groups)
                res.update({"ats": ats, "effort": "easy" if ats in EASY else "adapter", "locator": list(groups), **info})
                res["evidence"] = evidence + [f"confirmed {ats} {groups}"]
                return res
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and rate_limited is None:
                    rate_limited = (ats, groups)
                errors.append(f"{ats} {groups}: HTTP {e.response.status_code}")
            except Exception as e:
                errors.append(f"{ats} {groups}: {type(e).__name__}: {str(e)[:120]}")

        if rate_limited:  # locator found on the page/hint but the API refused us: keep it, mark unconfirmed
            ats, groups = rate_limited
            res.update({"ats": ats, "effort": "easy" if ats in EASY else "adapter", "locator": list(groups),
                        "status": "unconfirmed (HTTP 429, rerun later)", "total": "unknown", "india": "unknown"})
            res["evidence"] = evidence + errors
            return res

        for platform, val in (entry.get("hints") or {}).items():
            if platform in CUSTOM_PROBES:
                try:
                    info = CUSTOM_PROBES[platform](c, str(val))
                    res.update({"ats": "custom", "platform": platform, "effort": "custom-json", **info})
                    if entry.get("backend"):
                        res["backend"] = entry["backend"]
                    res["evidence"] = evidence + errors + [f"confirmed {platform} JSON"]
                    return res
                except Exception as e:
                    errors.append(f"{platform}: {type(e).__name__}: {str(e)[:120]}")

        if entry.get("platform"):  # known scrape-only platform from seed
            res.update({"ats": "custom", "platform": entry["platform"], "effort": "custom-scrape", "total": "unknown", "india": "unknown"})
            if entry.get("backend"):
                res["backend"] = entry["backend"]
            res["evidence"] = evidence + errors
            return res

        platforms = [p for p, pat in PLATFORMS if pat.search(final + "\n" + html)]
        if platforms:
            res.update({"ats": "custom", "platform": platforms[0], "effort": "custom-scrape", "total": "unknown", "india": "unknown"})
            if len(platforms) > 1:
                evidence.append(f"other platform markers: {platforms[1:]}")
        elif html:
            res.update({"ats": "custom", "platform": "unknown", "effort": "custom-scrape", "total": "unknown", "india": "unknown"})
            evidence.append("page loaded but no known ATS fingerprint (likely JS-rendered or in-house)")
        else:
            res.update({"ats": "unknown", "effort": "unknown", "total": "unknown", "india": "unknown"})
        res["evidence"] = evidence + errors
        return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="boards_seed.yaml")
    ap.add_argument("--out", default="boards.yaml")
    ap.add_argument("--only", action="append", help="classify only these names (no file written)")
    args = ap.parse_args(argv)

    seed = yaml.safe_load(Path(args.seed).read_text(encoding="utf-8"))
    entries = seed["boards"]
    if args.only:
        entries = [e for e in entries if e["name"] in args.only]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(classify, entries))

    for r in results:
        print(f"{r['name']:<16} {r['ats']:<16} {r.get('platform', ''):<14} total={r['total']!s:<6} india={r['india']!s:<6} {r['effort']}")
    if args.only:
        for r in results:
            print(yaml.safe_dump(r, sort_keys=False, allow_unicode=True))
        return 0

    by_effort = {k: [r["name"] for r in results if r["effort"] == k] for k in ("easy", "adapter", "custom-json", "custom-scrape", "unknown")}
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": by_effort,
        "boards": results,
    }
    Path(args.out).write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    print("\n" + "\n".join(f"{k}: {len(v)}  {', '.join(v)}" for k, v in by_effort.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
