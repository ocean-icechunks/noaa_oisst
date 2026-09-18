"""The ``monthly/`` group: per-variable min/max/mean/std statistics.

Real (computed, non-virtual) data, one timestep per month, written directly at
the final ``(24, 1, 90, 90)`` int16 chunking — there is no separate rechunk
pass. A month is rolled up only when every calendar day in it is present in
``daily/`` **and** none of them is still preliminary.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime

import icechunk
import icechunk.xarray
import numpy as np
import xarray as xr
import zarr

from ohw26_oisst_icechunk import config, store

logger = logging.getLogger(__name__)

# Retries for the month-load against transient NOAA fetch stalls.
LOAD_RETRIES = 3

# Default cap on concurrent virtual-chunk fetches while loading a month.
# zarr's default (~50) trips icechunk's stalled-stream protection on
# constrained networks; 10 is reliable and still loads a month in ~15 s.
# Override per run with --fetch-concurrency / OISST_FETCH_CONCURRENCY.
VIRTUAL_FETCH_CONCURRENCY = 10


# --- Statistics (pure) --------------------------------------------------------


def monthly_statistics(ds: xr.Dataset, month: date) -> xr.Dataset:
    """Reduce a month of daily OISST data to a single-timestep statistics Dataset.

    For each data variable in ``ds``, four real variables ``{var}_min`` /
    ``{var}_max`` / ``{var}_mean`` / ``{var}_std`` are produced by reducing over
    ``time``, then given a single new ``time`` coordinate at the month's start
    so the result can be appended along ``time`` to the monthly group.
    """
    stats: dict[str, xr.DataArray] = {}
    for var in ds.data_vars:
        stats[f"{var}_min"] = ds[var].min("time")
        stats[f"{var}_max"] = ds[var].max("time")
        stats[f"{var}_mean"] = ds[var].mean("time")
        stats[f"{var}_std"] = ds[var].std("time")

    reduced = xr.Dataset(stats)
    month_ts = np.datetime64(datetime(month.year, month.month, 1))  # noqa: DTZ001
    return reduced.expand_dims(time=[month_ts])


def months_ready(
    store_days: dict[date, bool],
    monthly_times: np.ndarray | None,
) -> list[date]:
    """Months eligible for rollup, as first-of-month dates, sorted.

    A month is ready when every calendar day in it is present in ``daily/``
    with ``preliminary=False``, and it is not already in the monthly group.
    (Completeness naturally excludes the in-progress month.)
    """
    by_month: dict[tuple[int, int], list[bool]] = {}
    for d, is_prelim in store_days.items():
        by_month.setdefault((d.year, d.month), []).append(is_prelim)

    ready = []
    for (year, month), flags in by_month.items():
        first = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]
        complete_and_final = len(flags) == days_in_month and not any(flags)
        if complete_and_final and not store.month_in_times(monthly_times, first):
            ready.append(first)
    return sorted(ready)


# --- Rollup write -------------------------------------------------------------


def rollup_month(
    repo: icechunk.Repository,
    month: date,
    fetch_concurrency: int = VIRTUAL_FETCH_CONCURRENCY,
) -> str | None:
    """Compute and commit one month's statistics from the daily group.

    Reads the referenced NOAA bytes for the month (a real reduction, not a
    virtual reference). Appends in calendar order; a month already present is
    overwritten in place via ``region="auto"``.
    """
    session = repo.readonly_session("main")
    daily = store.open_group(session, config.DAILY_GROUP)
    if daily is None:
        msg = "daily/ group does not exist; nothing to roll up"
        raise RuntimeError(msg)

    days_in_month = calendar.monthrange(month.year, month.month)[1]
    end = date(month.year, month.month, days_in_month)
    selected = daily.sel(time=slice(f"{month:%Y-%m-%d}", f"{end:%Y-%m-%d}"))
    num_days = selected.sizes.get("time", 0)
    if num_days != days_in_month:
        msg = f"{month:%Y-%m} has {num_days} of {days_in_month} days in daily/"
        raise RuntimeError(msg)
    if "preliminary" in selected and bool(
        np.any(np.asarray(selected["preliminary"].values))
    ):
        msg = f"{month:%Y-%m} still contains preliminary days; refusing to roll up"
        raise RuntimeError(msg)

    # Reading a month is ~120 concurrent virtual-chunk fetches from NOAA; a
    # transient stall on any one of them aborts the load, so retry a few times.
    last_error: Exception | None = None
    for attempt in range(1, LOAD_RETRIES + 1):
        try:
            with zarr.config.set({"async.concurrency": fetch_concurrency}):
                monthly_ds = monthly_statistics(
                    selected.drop_vars("preliminary", errors="ignore"), month
                ).load()
            break
        except icechunk.IcechunkError as err:  # pragma: no cover - network flake
            last_error = err
            logger.warning(
                "Load attempt %d/%d for %s failed: %s",
                attempt,
                LOAD_RETRIES,
                f"{month:%Y-%m}",
                str(err).splitlines()[0],
            )
    else:  # pragma: no cover - network flake
        msg = f"Failed to load {month:%Y-%m} after {LOAD_RETRIES} attempts"
        raise RuntimeError(msg) from last_error

    write_session = repo.writable_session("main")
    times = store.group_times(write_session, config.MONTHLY_GROUP)
    if times is None:
        encoding = config.monthly_encoding([str(name) for name in monthly_ds.data_vars])
        icechunk.xarray.to_icechunk(
            monthly_ds, write_session, group=config.MONTHLY_GROUP, encoding=encoding
        )
    elif store.month_in_times(times, month):
        logger.info("Overwriting existing monthly rollup for %s", f"{month:%Y-%m}")
        icechunk.xarray.to_icechunk(
            monthly_ds, write_session, group=config.MONTHLY_GROUP, region="auto"
        )
    else:
        latest = times.astype("datetime64[M]").max()
        if np.datetime64(month, "M") < latest:
            msg = (
                f"{month:%Y-%m} predates the monthly group's tail ({latest}); "
                "mid-record insertion is not supported"
            )
            raise RuntimeError(msg)
        icechunk.xarray.to_icechunk(
            monthly_ds, write_session, group=config.MONTHLY_GROUP, append_dim="time"
        )
    snapshot = write_session.commit(f"Monthly rollup for {month:%Y-%m}")
    logger.info("Committed monthly %s as snapshot %s", f"{month:%Y-%m}", snapshot)
    return snapshot
