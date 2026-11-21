# jobwatch state

Written by the detector, never by hand. Job IDs, timestamps and statuses only.

- `seen.json`: every posting ID ever seen, per board, with `first_seen` and a status.
- `health.json`: per board, whether the last fetch worked.

Code lives on `main`. The laptop (`detector/sync.py`) and the GitHub Actions fallback check this
branch out at `state/` and commit only here, so `main` holds code alone.
