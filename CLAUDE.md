# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

jobwatch sends fast job alerts and builds an application kit for one user (who they are, and the screening filter spec, are in the gitignored `CLAUDE.local.md`). The goal is to be among the first ~10 applicants on matching roles. Detection latency and minutes-to-submit are the levers.

## Hard constraints

- Only a Claude Pro subscription. No Anthropic API key, no paid infrastructure.
- Code does all repetitive and fast work: board enumeration, diffing, prefiltering, liveness. Claude only judges *new* roles, which keeps Pro limits from running out.
- The user always clicks the final submit. No auto-applications, no automatic outreach. Outreach goes out only after an explicit yes on recipient and wording, from a spare outreach inbox.
- `private/` (resume, answer bank, ledger, drafts) and `CLAUDE.local.md` are gitignored and must never reach the public repo (github.com/prithvimk/jobwatch). `state/` holds IDs and timestamps only.
- An unreachable or ambiguous source shows as a visible `unknown`, never a guess. Report a posting date only when the page states it. Otherwise say "first seen <detector run timestamp>".

## Commands

```powershell
uv sync                                                   # install deps (Python 3.13, uv)
uv run pytest -q                                          # all tests
uv run pytest tests/test_board_map.py::test_workday_india_nested_facet -q   # single test
uv run python -m detector.board_map                       # M0: boards_seed.yaml -> boards.yaml
uv run python -m detector.board_map --only Citi --only Visa   # re-probe some boards, print only (no write)
uv run python -m detector.run --dry-run                   # detector pass: prints pushes, writes no state
uv run python -m detector.run                             # live pass: pushes via ntfy, writes state/
uv run python -m detector.sync                            # laptop pass: pull, run (+screen), commit state, push, heartbeat, digest
uv run python -m detector.run --dry-run --screen          # dry pass that also calls Claude on each hit (uses Pro usage)
.\scripts\install_task.ps1                                 # register the 5-min Task Scheduler job (log: private/logs/sync.log)
```

To exercise the alert path without waiting for new postings, point `JOBWATCH_STATE_DIR` at a temp dir whose `seen.json` has a board with `baseline_at` set and empty `jobs`. Every current posting then counts as new. Pair this with `--dry-run`.

## Architecture (planned pipeline, built in milestones)

1. **Detector** (code, no LLM, every 10 min): polls each board in `boards.yaml`, diffs requisition IDs against the union of every ID ever seen (`state/`), applies a cheap title/location prefilter, checks that the apply URL is live, pushes title and link via ntfy, and commits only state.
2. **Screener** (Claude, after new IDs appear, at least hourly): fetches the full JD, applies the filter spec below, scores fit, names gaps, and pushes a verdict. Prompts live in `prompts/`.
3. **Application kit** (Claude, same run): the fit verdict with gaps, plus a resume tweak when one is worth making, saved to a doc the alert links to. No drafted screening answers: target boards ask form fields, not essays. `private/answers.md` holds only the facts forms ask that Simplify doesn't fill (notice, CTC, relocation, start date, years). Outreach drafts only on request.
4. **Submit** (user, ~2 min): Simplify autofill plus pasted kit answers.

Also required: an on-demand **sweep**. Given a company, it enumerates the full India board (all pages), pulls JDs for candidates, reports total counts, and lists filtered-out title hits with the reason.

Build order: M0 board map (done) → M1 detector for easy boards, hosted on the laptop every 5 min with public-repo GitHub Actions as the fallback (built; see `docs/hosting.md`) → M2 adapters, one company at a time, each with a fixture test (built for all 21 boards with a JSON source: Workday for 12 incl. Morgan Stanley, Deutsche Bank and Bank of America, Oracle for JPMC, Amazon, Eightfold for Microsoft, Jibe for MSCI and HERE, Atlassian; the 4 `custom-scrape` boards and SISC are not watched) → M3 screener (built; inputs `private/resume.md`, `private/answers.md`, the filter spec in `CLAUDE.local.md`) → M4 ledger, reminders, digests → M5 optional Claude-in-Chrome prefill that stops before submit.

## Board map (`detector/board_map.py`, `boards_seed.yaml` → `boards.yaml`)

- `boards_seed.yaml` is hand-maintained. `hints` are explicit ATS locators found by reading each careers page and a sample job page, and they are tried before HTML fingerprinting. Many corporate careers sites (Phenom, Radancy/TalentBrew, Jibe, Eightfold) are front ends. The apply button goes to the real ATS, so locate the backend from a **job detail page**, not the landing page.
- `effort` tiers: `easy` (Greenhouse/Lever/Ashby/SmartRecruiters public JSON), `adapter` (Workday CXS / Oracle HCM REST: one generic adapter, per-company config), `custom-json` (stable undocumented JSON: Amazon `search.json`, Eightfold `/api/pcsx/search`, Jibe `/api/jobs`, Atlassian listings, Deutsche Bank BeeSite), `custom-scrape` (HTML only).
- **Workday throttles per source IP across all tenants.** A burst of a few dozen requests caused 429s on every tenant for several minutes. All Workday calls are serialized under `WORKDAY_LOCK` and paced, and a 429 is recorded as `unconfirmed`, never as `unknown` ATS. Plan the detector cadence around this limit.
- Coverage notes: Sony means Sony Research India (openings are LinkedIn links on its WordPress careers page) plus Sony India Software Centre (no public board found, kept as `unknown`). Neither posts on Sony's global Workday. Prodigal and Zwayam were dropped and Bank of America added. Front ends whose apply button goes to Workday are tracked on Workday instead: Morgan Stanley (Eightfold → `ms.wd5 External`), Deutsche Bank (BeeSite → `db.wd3 DBWebsite`, 311 India vs 241 in BeeSite) and Bank of America (`ghr.wd1 lateral-ba_continuum`, a site holding only its India postings; its `jobssearchservlet` also lists them, with years of experience).
- India counts: Workday reads the country facet (`india_facet` gives the `appliedFacets` key/id to filter on), Oracle uses the `LOCATIONS` facet, and others filter location strings or use the API's country param.

## Detector (`detector/run.py`)

- Boards are read from `boards.yaml`. A board runs if its `ats` has a fetcher in `detector/sources.py:FETCHERS` (greenhouse, lever, ashby, smartrecruiters, workday, oracle, amazon, eightfold, jibe, atlassian). Custom-JSON boards (`ats: custom`) are keyed by their `platform` (`run.kind`). A fetcher is called as `fetcher(client, board_entry, skip=, location_ok=, full=)` and returns `Job`s; the JSON boards ignore the context, incremental ones use it to stop paging.
- `posted_at` is set only from a date the ATS states (Greenhouse `first_published`, Ashby `publishedAt`, SmartRecruiters `releasedDate`, Workday detail `startDate`, a date without a time; Oracle `PostedDate`, then the detail's exact `ExternalPostedStartDate`). Oracle also states a closing date (`ExternalPostedEndDate`); alerts show it, and JPMC postings often close within 1-2 weeks. Lever's `createdAt` is deliberately not used. Otherwise alerts say "first seen <run time>".
- Flow per pass: fetch → new IDs = not in `state/seen.json` (IDs are never deleted, so a repost under the same ID is never new) → prefilter (`detector/config.yaml`, inclusive by design; it returns a reason for every decision) → liveness (ATS detail API, JD page, apply page; closed-page redirects and closed text fail it) → ntfy push → save state. A board with no state is **baselined** silently on its first pass.
- A posting that fails liveness stays `pending` and is retried for `max_liveness_tries` passes, then becomes `dead`. A failed push stays pending too. SmartRecruiters' apply entry page (`/oneclick-ui/`) returns 403 to every non-browser client, so a redirect into it counts as resolved and is labeled that way.
- Workday (`detector/workday.py`): every call goes through system `curl`, paced 1 s apart, because Python TLS clients get 429. Lists are newest first, 20 per page, so a normal pass stops at the first page with nothing unseen or pending; each board gets a full sweep every 12 passes, rotated so only one board sweeps per pass. Country-facet tenants use `india_facet`; city-level tenants (Cisco, Mastercard, BlackRock, Barclays) filter on the allowed-city location values read from the first response. List rows saying "N Locations" have no locations until the detail API fills them in, and the prefilter is re-run then. The JD page is a JS shell that returns 200 even when closed, so the detail API (404 once closed, `canApply`) decides liveness.
- Oracle Recruiting Cloud (`detector/oracle.py`, locator = host + siteNumber): India via the `LOCATIONS` facet id, pages of 200 sorted newest first, so a full fetch every pass is 1-2 calls. The detail API returns no items for a closed posting and the job page is a JS shell, so the detail API decides liveness. ATSs needing a detail lookup register `(enrich, live)` in `sources.detail_hooks`.
- Amazon (`sources.amazon`, amazon.jobs `search.json`): `country=IND`, 100 per page, `sort=recent` (newest first only roughly within a day), incremental like Workday. `posted_date` is stated ("November 20, 2026"). A closed job's page returns 404. The apply link sends every job through an SSO redirect chain that drops non-browser clients, so for hosts in `run.SIGNIN_APPLY_HOSTS` only the first hop is checked.
- Eightfold (`detector/eightfold.py`, Microsoft): a fresh client's first call gets 429 and sets `_vs*` cookies, and bursts get 429, so calls are 3 s apart with one retry. Search is `location=India&sort_by=timestamp`, 10 per page, incremental; `postedTs` is the stated time. `position_details` 404s once closed and the job page is a JS shell, so it decides liveness. Morgan Stanley's Eightfold site is only a front end: its apply button goes to `ms.wd5.myworkdayjobs.com/External`, so it is tracked with the Workday adapter.
- Jibe (`sources.jibe`, MSCI and HERE): `<site>/api/jobs?location=India`, read in full every pass (tens of postings; `sortBy` returns 503). `posted_date` and, when present, `posting_expiry_date` are stated. A closed job's page returns 404 and the apply link is the iCIMS login page, so the generic liveness check works.
- Atlassian (`sources.atlassian`): one call lists every posting worldwide (~230); India is kept by location text. No posting date is stated, so alerts say "first seen". A closed iCIMS job page returns 410.
- `state/health.json` changes only when a board starts or stops failing, so healthy passes commit nothing. After `board_fail_alert_after` failures in a row, it pushes an "unreachable" alert.
- Two runners share `state/` through git: the laptop (`detector/sync.py`) and Actions. `state/*.json` merges by meaning via `detector/merge.py` (a git merge driver), so IDs are never lost when both commit. Actions skips while the repo variable `JOBWATCH_LOCAL_AT` (the laptop's heartbeat) is under 15 min old.
- The ntfy topic is a secret: anyone who knows it can read the alerts. Locally it is read from `private/ntfy.env`; in Actions it comes from the `NTFY_TOPIC` secret. With no topic set, pushes print instead of sending and postings stay pending.

## Screener (`detector/screen.py`, `prompts/screen.md`)

- Runs only on the laptop: `sync.py` turns it on when `screen.available()` (the `claude` CLI at `~/.local/bin`, `private/resume.md`, `private/answers.md`, and a `## Filter spec` section in `CLAUDE.local.md`). Actions never screens; its alerts say "Screening pending."
- One `claude -p --output-format json --max-turns 1 --model sonnet` call per live hit (`JOBWATCH_SCREEN_MODEL` overrides; about 10 s each), run in a temp dir outside the repo (so no CLAUDE.md is loaded) with tools disallowed. The prompt carries the spec, resume, facts and the full JD from `detector/jd.py` (ATS API text where available, else the JD page's text, and the source is named).
- The verdict comes only from the spec's exclusion rules; seniority uses the experience floor (≤3 or not stated IN, >3 to 5 STRETCH, ≥6 OUT). Degree and skill bars are gaps that lower `fit`, never OUT on their own. Checked on 12 live postings, 2026-11-21.
- Reply: one JSON object (`verdict` IN/STRETCH/OUT, `fit` 1-5, `location`, `experience_asked`, `one_line`, `gaps`, `out_reason`, `resume_tweak`). Verdicts are cached in `private/verdicts.json` by `board:id`, so a retried push never re-screens.
- IN/STRETCH: one push per role (title prefixed with the verdict, IN at priority 4), best fit first, and a kit in `private/kits/`. OUT: no push, state status `screened`, queued in `private/digest.json` and sent as one digest at the first laptop pass after 20:00 IST. Any screening failure (not logged in, timeout, bad reply) sends the plain alert with "Screening unavailable: <reason>"; a role is never dropped.
- The task's time limit is 20 min (`scripts/install_task.ps1`) because a pass with several hits makes several Claude calls.

## Filter spec

The screener's filter spec (location, seniority band, track, hard bars, excluded employers) is personal and lives in `CLAUDE.local.md`. The detector's cheap title/location prefilter is `detector/config.yaml`.

## Alerts and ledger

- Before any alert, the live JD page must load and the apply link must resolve to the entry page (sign-in/autofill). Check both at alert time.
- One message per role: company, title, location, in-band/STRETCH, a one-line fit with gaps, a direct link. Order by fit, then newest.
- Ledger keyed by exact requisition ID with states SHOWN / APPLIED / SKIPPED. APPLIED and SKIPPED are never re-recommended, even if reposted. Check history before calling anything "new".
- Follow-through: closing-date reminder ~2 days before close if there's no reaction, a weekly list of unresolved roles, one follow-up after 5 quiet days, and a Sunday digest of the skills the week's JDs asked for.
