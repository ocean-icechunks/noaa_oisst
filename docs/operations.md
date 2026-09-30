# Operations runbook

How the OISST Icechunk store is kept up to date, and what to do when something
needs a human. For why the store is shaped the way it is, see
[chunking.md](chunking.md).

## The one idea that makes everything else simple

Every command is _read the store's state → diff against what should be there →
do the missing work_. This means:

- **Any command can be killed and re-run.** Commits are atomic; an interrupted
  run leaves a valid store that the next run picks up from.
- **A failed or skipped scheduled run needs no retry policy** — _while the
  missing days are still inside the `--scan-months` window_: the next run does
  the same diff and catches up automatically. If a day falls out of that
  trailing window before it's ingested, `ingest-recent` refuses to append past
  the resulting hole and fails the job instead (see "Failure modes" below); the
  fix is `backfill-daily --start <tail+1>`.
- **Re-running when there is nothing to do is a no-op** - commands report
  "Nothing to do." and create no commit.

## Storage targets

Every command takes exactly one storage target, as CLI options or env vars:

| Option          | Env var             | Target                                                                   |
| --------------- | ------------------- | ------------------------------------------------------------------------ |
| `--store-path`  | `OISST_STORE_PATH`  | Local Icechunk store directory                                           |
| `--s3-bucket`   | `OISST_S3_BUCKET`   | S3 bucket (with the options below)                                       |
| `--s3-prefix`   | `OISST_S3_PREFIX`   | Key prefix, default `oisst`                                              |
| `--s3-region`   | `OISST_S3_REGION`   | Bucket region, default `us-east-1`                                       |
| `--s3-endpoint` | `OISST_S3_ENDPOINT` | Custom endpoint (e.g. Source.coop)                                       |
| `--s3-acl`      | `OISST_S3_ACL`      | Canned ACL on writes, default bucket-owner-full-control; `none` disables |

Flags for local work, env vars in Actions.

## Routine operation (automated)

[`daily.yml`](https://github.com/oceanhackweek/ohw26_oisst_icechunk/blob/main/.github/workflows/daily.yml)
runs four times a day (03:30, 09:30, 15:30, 21:30 UTC, plus `workflow_dispatch`)
and executes:

```bash
pixi run oisst ingest-recent  --scan-months 2
pixi run oisst rollup-monthly
```

- `ingest-recent` lists NOAA's trailing months, appends any new days (marked
  `preliminary=True` when only the preliminary file exists), and swaps
  preliminary days to final in place when NOAA has published the final file. A
  typical run completes in a few minutes; most runs have 0–1 appends and
  occasionally a swap.
- `rollup-monthly` computes the 16 monthly statistics for any month that is now
  complete and all-final. Most runs it does nothing; around mid-month one run
  finds a month ready (~31 files ≈ 50 MB from NOAA, ~5 min).

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
inactivity. None of that matters for a short gap — the next run diffs and
catches up. If the schedule got disabled, re-enable it from the Actions tab; if
it was disabled long enough that the store's tail has fallen outside
`ingest-recent`'s trailing `--scan-months` window, the next run's first
available day won't be contiguous with the tail, so it fails with
`ERROR: ... missing ...` instead of silently leaving a hole — repair with
`backfill-daily` over the gap (see "Failure modes" below).

**Backfilling a hole from the Actions tab.** `daily.yml` also takes
`workflow_dispatch` inputs `start` and `end` (both `YYYY-MM-DD`, `end`
optional). Leave both empty for a normal manual run (same as the schedule:
`ingest-recent`). Set `start` (and optionally `end`) to switch that run to
`backfill-daily --start <start> [--end <end>]` instead — e.g. to repair the hole
named in an `ingest-recent` `ERROR: ... missing ...` failure. The workflow never
passes `--create`, so this only works once the store already exists (see
"First-time setup" below).

## First-time setup

Every command except `backfill-daily --create` refuses to create a store —
`status`, `expire`, `rollup-monthly`, and `ingest-recent` all fail with
`ERROR: No Icechunk repository at ...; run backfill-daily --create` against a
target that doesn't exist yet, rather than silently creating one at a typo'd
path or bucket. Before the schedule (or any other command) can do anything,
create the store once by hand:

```bash
pixi run oisst backfill-daily --create --start 1981-09-01
```

## Manual commands

All of these work against any target; substitute `--store-path` for a local
copy. `status`, `expire`, and `rollup-monthly` require an existing store and
exit 1 (see "First-time setup" above) if there isn't one yet.

```bash
# What's in the store right now (day range, preliminary window, months);
# add --format markdown for the GitHub step-summary/issue version
pixi run oisst status

# Catch up daily data over the trailing months (what the scheduled Action runs)
pixi run oisst ingest-recent --scan-months 2

# Backfill a date range, committing per month (gap repair; add --create only
# for the very first run against a target, see "First-time setup" above)
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
store against the pinned icechunk 2.2.x — GC is the one genuinely destructive
operation here. Retention is 35 days; expiry costs time-travel history beyond
that window.

## Failure modes and what they mean

- **A reader hits a missing chunk on a preliminary day.** NOAA deleted the
  preliminary file when the final landed, and the next scheduled run hasn't
  swapped the reference yet (≤ ~24 h window). No action needed — the next run
  fixes it; readers can use `.sel(preliminary=False)` meanwhile.
- **`WARNING: skipping YYYY-MM-DD - older than the store's tail (gap)`** from
  `ingest-recent`: a day appeared at NOAA that predates the store's newest day
  and isn't in the store. Appending it would break calendar order, so it is
  skipped. Repair with `backfill-daily` over a range covering the gap.
- **`ERROR: store tail is ... missing ...`** (exit 1) from `ingest-recent` /
  `backfill-daily`: the first day NOAA has available is _after_ the store's
  tail, with a gap of one or more days NOAA doesn't have yet in between.
  Appending past the hole would leave `daily/` permanently non-contiguous, so
  the run refuses and fails visibly instead. This is expected right after an
  outage at NOAA; the run keeps failing (harmlessly — the swap step still runs)
  until NOAA backfills the missing day(s), at which point the very next run
  appends normally. To force it sooner, or if the day(s) will never appear at
  that URL, run the recovery command the error prints:
  `oisst backfill-daily --start <tail+1>`.
- **A workflow run fails outright** (network, NOAA outage, runner death): do
  nothing. Commits are atomic, so the store is valid; the next run catches up.
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
