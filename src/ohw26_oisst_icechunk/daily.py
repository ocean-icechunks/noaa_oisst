"""The ``daily/`` group: virtual appends, the prelim→final swap, `preliminary` coord.

The group holds one timestep per day, calendar-ordered by construction:
appends always land at the end in date order, and final-file swaps are
in-place ``set_virtual_ref`` calls that leave the time
index untouched and flip the day's ``preliminary`` flag.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import xarray as xr
import zarr

if TYPE_CHECKING:
    from collections.abc import Sequence

    import icechunk

from ohw26_oisst_icechunk import config, sources, store

logger = logging.getLogger(__name__)

# How many NOAA metadata fetches to run concurrently when building a batch.
DEFAULT_WORKERS = 16


# --- Virtual dataset construction --------------------------------------------


def object_store_registry() -> Any:
    """VirtualiZarr ``ObjectStoreRegistry`` for the anonymous NOAA bucket."""
    # pylint: disable=import-outside-toplevel
    from obspec_utils.registry import ObjectStoreRegistry  # noqa: PLC0415
    from obstore.store import S3Store  # noqa: PLC0415

    s3 = S3Store.from_url(
        sources.NODD_STORE_PREFIX, region=sources.NODD_REGION, skip_signature=True
    )
    return ObjectStoreRegistry({sources.NODD_STORE_PREFIX: s3})


def open_day(d: date, preliminary: bool, registry: Any | None = None) -> xr.Dataset:
    """Parse one day's NetCDF into a (metadata-only) virtual dataset."""
    # pylint: disable=import-outside-toplevel
    from virtualizarr import open_virtual_dataset  # noqa: PLC0415
    from virtualizarr.parsers import HDFParser  # noqa: PLC0415

    if registry is None:
        registry = object_store_registry()
    url = sources.url_for_date(d, preliminary=preliminary)
    return open_virtual_dataset(url, registry=registry, parser=HDFParser())


def build_batch(
    days: list[tuple[date, bool]],
    max_workers: int = DEFAULT_WORKERS,
) -> xr.Dataset:
    """Build the concatenated virtual dataset for a batch of ``(day, preliminary)``.

    Metadata-only, so a month of files fits trivially in memory; NOAA fetches
    run ``max_workers``-wide, concatenation preserves the given (calendar)
    order, and the ``preliminary`` flags land as a real bool coordinate.
    """
    registry = object_store_registry()
    with ThreadPoolExecutor(max_workers) as pool:
        vdss = list(pool.map(lambda dp: open_day(dp[0], dp[1], registry), days))
    vds = xr.concat(
        vdss,
        dim="time",
        coords="minimal",
        compat="override",
        combine_attrs="override",
    )
    flags = np.array([preliminary for _, preliminary in days], dtype=bool)
    return vds.assign_coords(preliminary=("time", flags))


# --- Work planning (pure) -----------------------------------------------------


@dataclass(frozen=True)
class DailyWork:
    """What a daily-ingest run needs to do, diffed against the store."""

    # Days to append, calendar-ordered, with their preliminary flag.
    append: list[tuple[date, bool]] = field(default_factory=list)
    # Days present as preliminary whose final file is now available.
    swap: list[date] = field(default_factory=list)
    # Available days older than the store's tail that cannot be appended
    # (mid-record gaps need insertion, which we deliberately don't do).
    skipped_gaps: list[date] = field(default_factory=list)
    # Days strictly between the store's tail and the first appendable day
    # that NOAA doesn't have yet. Appending past them would leave a
    # permanent hole in daily/, so append is cleared until they arrive.
    missing: list[date] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Whether there is any work at all."""
        return bool(self.append or self.swap)


def plan_daily_work(
    store_days: dict[date, bool],
    final_available: set[date],
    prelim_available: set[date],
) -> DailyWork:
    """Diff available NOAA files against store state.

    ``store_days`` maps each day already in the store to its ``preliminary``
    flag. A day gets appended with its best available flavor (final wins);
    a stored preliminary day whose final now exists gets swapped in place.
    """
    swap = sorted(
        d for d, is_prelim in store_days.items() if is_prelim and d in final_available
    )

    tail = max(store_days) if store_days else None
    append: list[tuple[date, bool]] = []
    skipped: list[date] = []
    for d in sorted(final_available | prelim_available):
        if d in store_days:
            continue
        if tail is not None and d < tail:
            skipped.append(d)
            continue
        append.append((d, d not in final_available))

    missing: list[date] = []
    if tail is not None and append and append[0][0] != tail + timedelta(days=1):
        gap_days = (append[0][0] - tail).days - 1
        missing = [tail + timedelta(days=i) for i in range(1, gap_days + 1)]
        append = []

    return DailyWork(append=append, swap=swap, skipped_gaps=skipped, missing=missing)


def is_calendar_ordered(days: Sequence[date]) -> bool:
    """Whether ``days`` is strictly increasing (no ties, no inversions)."""
    return all(a < b for a, b in pairwise(days))


def store_days_state(session: icechunk.Session) -> dict[date, bool]:
    """Read the store's ``{day: preliminary flag}`` state (empty if no group)."""
    ds = store.open_group(session, config.DAILY_GROUP)
    if ds is None or "time" not in ds:
        return {}
    days = store.times_to_days(np.asarray(ds["time"].values))
    if "preliminary" in ds:
        flags = np.asarray(ds["preliminary"].values).astype(bool)
    else:
        flags = np.zeros(len(days), dtype=bool)
    return dict(zip(days, flags, strict=True))


# --- Writes -------------------------------------------------------------------


def append_batch(
    repo: icechunk.Repository,
    days: list[tuple[date, bool]],
    max_workers: int = DEFAULT_WORKERS,
) -> str | None:
    """Append a calendar-ordered batch of days to ``daily/`` and commit.

    Returns the snapshot id, or ``None`` when ``days`` is empty.
    """
    if not days:
        return None
    vds = build_batch(days, max_workers=max_workers)
    vds["preliminary"].encoding["chunks"] = config.PRELIMINARY_CHUNKS
    # Only takes effect on creation; appends keep the existing chunk grid.
    vds["time"].encoding["chunks"] = config.TIME_CHUNKS

    session = repo.writable_session("main")
    exists = store.group_times(session, config.DAILY_GROUP) is not None
    if exists:
        vds.vz.to_icechunk(session.store, group=config.DAILY_GROUP, append_dim="time")
    else:
        vds.vz.to_icechunk(session.store, group=config.DAILY_GROUP)
    first, last = days[0][0], days[-1][0]
    label = (
        f"{first:%Y-%m-%d}" if first == last else f"{first:%Y-%m-%d}..{last:%Y-%m-%d}"
    )
    snapshot = session.commit(f"Append {len(days)} day(s) {label} to daily/")
    logger.info("Appended %d day(s) %s as snapshot %s", len(days), label, snapshot)
    return snapshot


def swap_to_final(
    repo: icechunk.Repository,
    days: list[date],
    max_workers: int = DEFAULT_WORKERS,
) -> str | None:
    """Replace preliminary days' virtual references with their final files.

    In-place: four ``set_virtual_ref`` calls per day (each OISST file holds
    exactly one chunk per variable) plus a ``preliminary`` flag flip, in a
    single commit. The time index is untouched.
    """
    if not days:
        return None

    registry = object_store_registry()
    with ThreadPoolExecutor(max_workers) as pool:
        finals = list(pool.map(lambda d: open_day(d, False, registry), days))

    session = repo.writable_session("main")
    times = store.group_times(session, config.DAILY_GROUP)
    if times is None:
        msg = "daily/ group does not exist; nothing to swap"
        raise RuntimeError(msg)
    day_index = {d: i for i, d in enumerate(store.times_to_days(times))}

    group = zarr.open_group(session.store, path=config.DAILY_GROUP, mode="a")
    prelim_array = cast("zarr.Array[Any]", group["preliminary"])
    for d, vds in zip(days, finals, strict=True):
        i = day_index.get(d)
        if i is None:
            msg = f"{d:%Y-%m-%d} is not in the daily/ time index"
            raise RuntimeError(msg)
        for name in config.VARIABLES:
            manifest = vds[name].data.manifest.dict()
            if set(manifest) != {"0.0.0.0"}:
                msg = (
                    f"{name} in {sources.url_for_date(d)} has chunk grid {sorted(manifest)}; "
                    "expected the single-chunk layout the swap relies on"
                )
                raise RuntimeError(msg)
            ref = manifest["0.0.0.0"]
            session.store.set_virtual_ref(
                f"{config.DAILY_GROUP}/{name}/c/{i}/0/0/0",
                ref["path"],
                offset=ref["offset"],
                length=ref["length"],
            )
        prelim_array[i] = False

    label = ", ".join(f"{d:%Y-%m-%d}" for d in days)
    snapshot = session.commit(f"Final files replace preliminary for {label}")
    logger.info("Swapped %d day(s) to final as snapshot %s", len(days), snapshot)
    return snapshot
