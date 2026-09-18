"""End-to-end tests against NOAA's public bucket (run with ``pytest -m network``).

Small on purpose: three days of virtual appends, the in-place swap, and the
skill's final check - a store slice must equal the same data read straight from
the original NOAA NetCDF with xarray.
"""

from __future__ import annotations

import io
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
import pytest
import xarray as xr
import zarr

if TYPE_CHECKING:
    from pathlib import Path

from ohw26_oisst_icechunk import config, daily, sources, store

pytestmark = pytest.mark.network

DAYS = [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]


def _noaa_direct(d: date) -> xr.Dataset:
    fs = sources.anon_s3()
    data = fs.cat(sources.url_for_date(d))
    return xr.open_dataset(io.BytesIO(data), engine="h5netcdf").load()


def test_virtual_append_swap_and_read_back(tmp_path: Path) -> None:
    target = store.StoreTarget(local_path=tmp_path / "store")
    repo = store.open_or_create_repo(target)

    # append three final days, then artificially mark day 2 preliminary
    daily.append_batch(repo, [(d, False) for d in DAYS])
    session = repo.writable_session("main")
    prelim = zarr.open_group(session.store, path=config.DAILY_GROUP, mode="a")[
        "preliminary"
    ]
    assert isinstance(prelim, zarr.Array)
    prelim[1] = True
    session.commit("pretend day 2 is preliminary")

    state = daily.store_days_state(repo.readonly_session("main"))
    assert state[DAYS[1]]

    # swap it to final: refs rewritten in place, flag flipped, time index unchanged
    daily.swap_to_final(repo, [DAYS[1]])
    session = repo.readonly_session("main")
    ds = store.open_group(session, config.DAILY_GROUP)
    assert ds is not None
    assert ds.sizes["time"] == 3
    assert not ds["preliminary"].values.any()
    times = np.asarray(ds["time"].values).astype("datetime64[D]")
    assert list(times) == [np.datetime64(d) for d in DAYS]

    # the skill's final check: store slice == direct NOAA read
    truth = _noaa_direct(DAYS[1])
    for var in config.VARIABLES:
        a = ds[var].isel(time=1).values
        b = truth[var].isel(time=0).values
        assert np.array_equal(np.isnan(a), np.isnan(b))
        both = ~np.isnan(a)
        # float32 vs float64 decode of NOAA's float32 scale_factor
        assert np.allclose(a[both], b[both], atol=1e-5)


def test_rerun_of_planner_is_a_noop(tmp_path: Path) -> None:
    target = store.StoreTarget(local_path=tmp_path / "store")
    repo = store.open_or_create_repo(target)
    daily.append_batch(repo, [(d, False) for d in DAYS])

    state = daily.store_days_state(repo.readonly_session("main"))
    work = daily.plan_daily_work(state, set(DAYS), set())
    assert not work
