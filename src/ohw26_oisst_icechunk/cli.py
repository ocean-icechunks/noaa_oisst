"""The ``oisst`` command-line interface (Typer).

Every command is *read the store's state → diff against what should be there →
do the missing work*.

The storage target comes from global options with env-variable fallbacks
(``--store-path`` / ``OISST_STORE_PATH``, ``--s3-bucket`` / ``OISST_S3_BUCKET``,
...), so the same commands run locally with flags and in Actions with env vars.
"""

from __future__ import annotations

import logging
import warnings
from datetime import UTC, date, datetime, timedelta
from pathlib import Path  # noqa: TC003 - typer needs it at runtime
from typing import Annotated

import icechunk
import typer
import xarray as xr

from ohw26_oisst_icechunk import config, daily, maintenance, rollup, sources, store

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="oisst",
    help="Maintain the NOAA OISST v2.1 Icechunk store.",
    no_args_is_help=True,
)


@app.callback()
def main_options(
    ctx: typer.Context,
    store_path: Annotated[
        Path | None,
        typer.Option(envvar="OISST_STORE_PATH", help="Local Icechunk store directory."),
    ] = None,
    s3_bucket: Annotated[
        str | None,
        typer.Option(envvar="OISST_S3_BUCKET", help="Destination S3 bucket."),
    ] = None,
    s3_prefix: Annotated[
        str,
        typer.Option(envvar="OISST_S3_PREFIX", help="Key prefix inside the bucket."),
    ] = "oisst",
    s3_region: Annotated[
        str,
        typer.Option(envvar="OISST_S3_REGION", help="Destination bucket region."),
    ] = "us-east-1",
    s3_endpoint: Annotated[
        str | None,
        typer.Option(
            envvar="OISST_S3_ENDPOINT",
            help="Custom S3 endpoint (e.g. https://data.source.coop).",
        ),
    ] = None,
    s3_acl: Annotated[
        str,
        typer.Option(
            envvar="OISST_S3_ACL",
            help="Canned ACL on every object written (Source.coop cross-account "
            "uploads need bucket-owner-full-control); pass an empty string or "
            "'none' to send none.",
        ),
    ] = "bucket-owner-full-control",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Resolve the storage target shared by every subcommand."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    icechunk.set_logs_filter("info" if verbose else "error")
    warnings.filterwarnings(
        "ignore",
        message="Numcodecs codecs are not in the Zarr version 3 specification",
        category=UserWarning,
    )
    warnings.filterwarnings(
        "ignore",
        category=xr.coding.variables.SerializationWarning,
    )
    try:
        ctx.obj = store.StoreTarget(
            local_path=store_path,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            s3_region=s3_region,
            s3_endpoint=s3_endpoint,
            s3_acl=None if s3_acl.strip().lower() in {"", "none"} else s3_acl,
        )
    except ValueError as err:
        raise typer.BadParameter(str(err)) from err


def _echo_work(work: daily.DailyWork) -> None:
    """Report a planned diff to the terminal."""
    typer.echo(f"To append: {len(work.append)}  to swap prelim→final: {len(work.swap)}")
    for d in work.skipped_gaps:
        typer.echo(
            f"WARNING: skipping {d:%Y-%m-%d} - older than the store's tail (gap)"
        )
    if work.missing:
        tail = work.missing[0] - timedelta(days=1)
        first_available = work.missing[-1] + timedelta(days=1)
        recovery = tail + timedelta(days=1)
        typer.echo(
            f"ERROR: store tail is {tail:%Y-%m-%d} but the next available day at "
            f"NOAA is {first_available:%Y-%m-%d}; missing "
            f"{work.missing[0]:%Y-%m-%d}..{work.missing[-1]:%Y-%m-%d}. Refusing to "
            "append past the hole. Recover with "
            f"`oisst backfill-daily --start {recovery:%Y-%m-%d}`."
        )


def _open_repo(
    target: store.StoreTarget, *, create: bool = False
) -> icechunk.Repository:
    """Open the repo, turning a missing store into a clean CLI exit."""
    try:
        return store.open_repo(target, create=create)
    except FileNotFoundError as err:
        typer.echo(f"ERROR: {err}")
        raise typer.Exit(1) from err


def _run_daily_work(
    target: store.StoreTarget,
    months: list[str],
    commit_batch_months: bool,
    *,
    create: bool = False,
) -> None:
    """Shared daily-ingest body: open the store, list NOAA months, diff, append + swap."""
    # Open the store before listing NOAA so a missing store fails fast (and
    # without any network traffic).
    repo = _open_repo(target, create=create)

    fs = sources.anon_s3()
    finals = set(sources.dates_available(fs, preliminary=False, months=months))
    prelims = set(sources.dates_available(fs, preliminary=True, months=months))

    session = repo.readonly_session("main")
    state = daily.store_days_state(session)
    work = daily.plan_daily_work(state, finals, prelims)
    _echo_work(work)
    if not work and not work.missing:
        typer.echo("Nothing to do.")
        return

    if commit_batch_months:
        by_month: dict[str, list[tuple[date, bool]]] = {}
        for d, flag in work.append:
            by_month.setdefault(f"{d:%Y%m}", []).append((d, flag))
        for month_key in sorted(by_month):
            daily.append_batch(repo, by_month[month_key])
    else:
        daily.append_batch(repo, work.append)

    daily.swap_to_final(repo, work.swap)

    if work.missing:
        raise typer.Exit(1)


@app.command()
def backfill_daily(
    ctx: typer.Context,
    start: Annotated[
        str, typer.Option(help="First day, YYYY-MM-DD.")
    ] = config.START_DATE.isoformat(),
    end: Annotated[
        str | None, typer.Option(help="Last day, YYYY-MM-DD (default: today).")
    ] = None,
    create: Annotated[
        bool,
        typer.Option(
            help="Create the store if it doesn't exist yet. backfill-daily is "
            "the only command that may create a store."
        ),
    ] = False,
) -> None:
    """Backfill the daily/ group over a date range, committing per month."""
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(tz=UTC).date()
    months = sources.months_between(start_date, end_date)
    typer.echo(f"Backfilling {months[0]}..{months[-1]} ({len(months)} months)")
    _run_daily_work(ctx.obj, months, commit_batch_months=True, create=create)


@app.command()
def ingest_recent(
    ctx: typer.Context,
    scan_months: Annotated[
        int, typer.Option(help="How many trailing months of NOAA listings to scan.")
    ] = 2,
) -> None:
    """Append new days and swap preliminary→final over the trailing months."""
    months = sources.recent_months(datetime.now(tz=UTC), count=scan_months)
    _run_daily_work(ctx.obj, months, commit_batch_months=False, create=False)


@app.command()
def rollup_monthly(
    ctx: typer.Context,
    month: Annotated[
        str | None,
        typer.Option(
            help="Roll up one specific month (YYYY-MM) instead of catching up."
        ),
    ] = None,
    fetch_concurrency: Annotated[
        int,
        typer.Option(
            envvar="OISST_FETCH_CONCURRENCY",
            help="Concurrent virtual-chunk fetches while loading a month "
            "(lower this on constrained networks).",
        ),
    ] = rollup.VIRTUAL_FETCH_CONCURRENCY,
) -> None:
    """Compute monthly statistics for every complete, all-final month."""
    repo = _open_repo(ctx.obj)
    session = repo.readonly_session("main")

    if month is not None:
        months = [date.fromisoformat(f"{month}-01")]
    else:
        state = daily.store_days_state(session)
        monthly_times = store.group_times(session, config.MONTHLY_GROUP)
        months = rollup.months_ready(state, monthly_times)

    if not months:
        typer.echo("Nothing to do.")
        return
    typer.echo(
        f"Rolling up {len(months)} month(s): {months[0]:%Y-%m}..{months[-1]:%Y-%m}"
    )
    failed: list[date] = []
    for m in months:
        try:
            rollup.rollup_month(repo, m, fetch_concurrency=fetch_concurrency)
        except RuntimeError as err:
            logger.exception("Rollup failed for %s", f"{m:%Y-%m}")
            typer.echo(f"WARNING: rollup failed for {m:%Y-%m}: {err}")
            failed.append(m)

    if failed:
        names = ", ".join(f"{m:%Y-%m}" for m in failed)
        typer.echo(f"ERROR: rollup failed for {len(failed)} month(s): {names}")
        raise typer.Exit(1)


@app.command()
def expire(
    ctx: typer.Context,
    days: Annotated[int, typer.Option(help="Retention window in days.")] = 35,
    dry_run: Annotated[bool, typer.Option(help="Report without deleting.")] = False,
) -> None:
    """Expire snapshots older than the retention window and garbage-collect."""
    repo = _open_repo(ctx.obj)
    summary = maintenance.expire(repo, days=days, dry_run=dry_run)
    typer.echo(str(summary))


@app.command()
def status(ctx: typer.Context) -> None:
    """Summarize the store's state (day counts, preliminary window, months)."""
    repo = _open_repo(ctx.obj)
    session = repo.readonly_session("main")

    state = daily.store_days_state(session)
    if state:
        days = sorted(state)
        prelim = [d for d, flag in state.items() if flag]
        monotonic = "yes" if daily.is_calendar_ordered(list(state)) else "NO"
        typer.echo(
            f"daily/    {len(days)} days  {days[0]:%Y-%m-%d}..{days[-1]:%Y-%m-%d}  "
            f"preliminary: {len(prelim)}  calendar-ordered: {monotonic}"
        )
        if prelim:
            typer.echo(
                f"          preliminary days: {min(prelim):%Y-%m-%d}..{max(prelim):%Y-%m-%d}"
            )
    else:
        typer.echo("daily/    (empty)")

    monthly_times = store.group_times(session, config.MONTHLY_GROUP)
    if monthly_times is not None and len(monthly_times):
        months = monthly_times.astype("datetime64[M]")
        typer.echo(f"monthly/  {len(months)} months  {months.min()}..{months.max()}")
    else:
        typer.echo("monthly/  (empty)")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
