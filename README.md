# github-repo-monitor

A dependency-light GitHub repository activity monitor. It watches one or more
repos for new commits, issue/PR activity (and their comments), and releases,
then classifies each item into **ALERT / CONTEXT / ERROR** tiers and prints a
Markdown digest. By default it does **zero remote writes** — no issues created,
no comments posted, no pushes — so it runs anywhere (a GitHub Actions cron, a
local cron job, or your laptop) against only the public GitHub REST API.

There are two monitor scripts:

- `scripts/gh_monitor.py` — a generic, parameter-driven monitor. Point it at any
  `owner/repo` (plus optional path / keyword / label / author filters) via the
  `MONITOR_REPOS` config block near the top, and it aggregates the activity for
  you. Pure stdlib, no dependencies, no GitHub token required (anonymous
  60 req/hr covers small setups; a `GITHUB_TOKEN` raises the rate limit).
- `scripts/magpie_monitor.py` — a focused monitor for the Magpie ecosystem
  (`yetone/magpie` + `magpie-community/plugins`) using the same cursor /
  de-duplication / triage model, with an optional tracking-issue state backend.

## Run

```bash
python scripts/gh_monitor.py                              # incremental run from stored cursor
python scripts/gh_monitor.py --since 2026-11-01T00:00:00Z   # rewind and replay from a date
python scripts/gh_monitor.py --lookback-days 14            # first run: look back 14 days
python scripts/gh_monitor.py --dry-run                     # report only, don't advance cursor
python scripts/gh_monitor.py --report gh_report.md          # also write the report to a file
```

State is persisted to a local JSON file (`gh_monitor_state.json` by default);
the cursor only moves forward, so re-runs never duplicate reports. Edit
`MONITOR_REPOS` to choose what you watch.

## Manual runs (GitHub Actions)

`.github/workflows/magpie-monitor.yml` runs the monitor on demand via
`workflow_dispatch` only — there is **no scheduled / daily cron** by default.
Trigger it from the Actions tab, optionally passing a `since` ISO-8601 UTC
cursor to rewind for a deep digest. To restore daily runs, add a `schedule`
entry under `on:` in that workflow file. It runs `scripts/magpie_monitor.py`
by default; switch the `run:` step to `scripts/gh_monitor.py` if you prefer the
generic monitor. The workflow grants only `contents: read` and `issues: write`,
and posts nothing unless posting is explicitly enabled — consistent with the
zero-write-by-default design.

## License

MIT — see [LICENSE](LICENSE).
