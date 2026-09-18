# Operations runbook

How the OISST Icechunk store is kept up to date, and what to do when something
needs a human. For why the store is shaped the way it is, see
[chunking.md](chunking.md).

## The one idea that makes everything else simple

Every command is _read the store's state → diff against what should be there →
do the missing work_. This means:

- **Any command can be killed and re-run.** Commits are atomic; an interrupted
  run leaves a valid store that the next run picks up from.
- **A failed or skipped scheduled run needs no retry policy.** The next run does
  the same diff and catches up automatically.
- **Re-running when there is nothing to do is a no-op** - commands report
  "Nothing to do." and create no commit.

## Storage targets

Every command takes exactly one storage target, as CLI options or env vars:

| Option          | Env var             | Target                                                                  |
| --------------- | ------------------- | ----------------------------------------------------------------------- |
| `--store-path`  | `OISST_STORE_PATH`  | Local Icechunk store directory                                          |
| `--s3-bucket`   | `OISST_S3_BUCKET`   | S3 bucket (with the options below)                                      |
| `--s3-prefix`   | `OISST_S3_PREFIX`   | Key prefix, default `oisst`                                             |
| `--s3-region`   | `OISST_S3_REGION`   | Bucket region, default `us-east-1`                                      |
| `--s3-endpoint` | `OISST_S3_ENDPOINT` | Custom endpoint (e.g. Source.coop)                                      |
| `--s3-acl`      | `OISST_S3_ACL`      | Canned ACL on writes, default bucket-owner-full-control; empty disables |

Flags for local work, env vars in Actions.

## Routine operation (automated)

[`daily.yml`](https://github.com/oceanhackweek/ohw26_oisst_icechunk/blob/main/.github/workflows/daily.yml)
runs once a day (plus `workflow_dispatch`) and executes:

```bash
pixi run oisst ingest-recent  --scan-months 2
pixi run oisst rollup-monthly
```

- `ingest-recent` lists NOAA's trailing months, appends any new days (marked
  `preliminary=True` when only the preliminary file exists), and swaps
  preliminary days to final in place when NOAA has published the final file.
  Typical day completes in a few minutes as it has 1–3 appends and ~1 swap.
- `rollup-monthly` computes the 16 monthly statistics for any month that is now
  complete and all-final. Most days it does nothing; around mid-month one month
  becomes ready (~31 files ≈ 50 MB from NOAA, ~5 min).

One writer, always: the workflow uses
`concurrency: {group: oisst-store, cancel-in-progress: false}`, so there is no
commit-conflict handling anywhere in the code — do not run a second writer
against the production store while a workflow run is active.

The workflow **skips itself** unless the `OISST_S3_BUCKET` repository variable
is set, so the schedule is safe to have enabled before the Stage 2 destination
exists. Configuring production = setting the repo variables (`OISST_S3_BUCKET`,
optionally `OISST_S3_PREFIX` / `OISST_S3_REGION` / `OISST_S3_ENDPOINT` /
`OISST_S3_ACL`, and `OISST_AWS_ROLE_ARN` for OIDC) — no workflow edits.

Best-effort cron is fine by design: `on: schedule` fires only from the default
branch, runs late under load, and GitHub disables it after 60 days of repo
inactivity. None of that matters, the next run diffs and catches up. If the
schedule got disabled, re-enable it from the Actions tab.

## Manual commands

All of these work against any target; substitute `--store-path` for a local
copy.

```bash
# What's in the store right now (day range, preliminary window, months)
pixi run oisst status

# Catch up daily data over the trailing months (what the Action runs)
pixi run oisst ingest-recent --scan-months 2

# Backfill a date range, committing per month (initial load / gap repair)
pixi run oisst backfill-daily --start 1981-09-01 --end 2020-12-31

# Roll up every ready month, or one specific month
pixi run oisst rollup-monthly
pixi run oisst rollup-monthly --month 2026-07

# Snapshot expiry + GC — see the warning below
pixi run oisst expire --days 35 --dry-run
```

`--verbose` / `-v` turns debug logging (including icechunk's internal logs) back
on; by default only errors from the storage layer are shown.

On constrained networks, lower `--fetch-concurrency` / `OISST_FETCH_CONCURRENCY`
(default 10) for `rollup-monthly` — a month's rollup is ~120 virtual-chunk
range-reads against NOAA.

## Expiry and garbage collection

Each monthly rollup rewrites the trailing 24-month slab, shedding ~9.6 GB/year
of superseded chunks, so periodic expiry + GC is required.

**Not yet enabled on a schedule.**
[`maintenance.yml`](https://github.com/oceanhackweek/ohw26_oisst_icechunk/blob/main/.github/workflows/maintenance.yml)
is `workflow_dispatch`-only and defaults to `--dry-run`. Before enabling the
weekly cron (or running without `--dry-run` against production), the
GC-vs-virtual-references check in TASKS.md must pass on a throwaway copy of the
store against the pinned icechunk 2.0.x — GC is the one genuinely destructive
operation here. Retention is 35 days; expiry costs time-travel history beyond
that window.

## Failure modes and what they mean

- **A reader hits a missing chunk on a preliminary day.** NOAA deleted the
  preliminary file when the final landed, and the next daily run hasn't swapped
  the reference yet (≤ ~24 h window). No action needed — the next run fixes it;
  readers can use `.sel(preliminary=False)` meanwhile.
- **`WARNING: skipping YYYY-MM-DD - older than the store's tail (gap)`** from
  `ingest-recent`: a day appeared at NOAA that predates the store's newest day
  and isn't in the store. Appending it would break calendar order, so it is
  skipped. Repair with `backfill-daily` over a range covering the gap.
- **A workflow run fails outright** (network, NOAA outage, runner death): do
  nothing. Commits are atomic, so the store is valid; tomorrow's run catches up.
  Re-run manually via `workflow_dispatch` only if you're impatient.
- **`rollup-monthly` won't produce a month you expect**: the month has a missing
  or still-preliminary day — `oisst status` shows the preliminary window.
  Rollups only consume complete, all-final months, by design.

## Credentials

`ci.yml` runs on `pull_request` with **no credentials** and must stay that way
(zizmor lints the workflows). Credentials appear only in `daily.yml` /
`maintenance.yml`, which run on `schedule`/`workflow_dispatch` — triggers forks
cannot fire. Preferred mechanism is GitHub OIDC (`id-token: write` +
`aws-actions/configure-aws-credentials` with the role in the
`OISST_AWS_ROLE_ARN` repo variable); no long-lived keys.
