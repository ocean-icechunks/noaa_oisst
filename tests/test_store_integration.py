"""Local-store integration tests with synthetic (non-virtual) data.

These cover the store-facing behavior that doesn't need NOAA: the
``preliminary`` coordinate round-trip, rollup gating and encoding, and
idempotent re-runs - all against ``local_filesystem_storage`` in ``tmp_path``.
The virtual paths (appends, the ``set_virtual_ref`` swap) are exercised by the
network-marked end-to-end test and the CLI loop.
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import icechunk
import icechunk.xarray
import numpy as np
import pytest
import xarray as xr
import zarr

from ohw26_oisst_icechunk import config, rollup, store
from ohw26_oisst_icechunk.daily import store_days_state


def _synthetic_daily(year: int, month: int, preliminary_days: set[int]) -> xr.Dataset:
    """One month of tiny OISST-shaped daily data (time, zlev, lat, lon)."""
    ndays = calendar.monthrange(year, month)[1]
    rng = np.random.default_rng(42)
    times = np.array(
        [f"{year}-{month:02d}-{d:02d}T12:00" for d in range(1, ndays + 1)],
        dtype="datetime64[ns]",
    )
    shape = (ndays, 1, 4, 4)
    data_vars = {
        var: (("time", "zlev", "lat", "lon"), rng.random(shape).astype(np.float32))
        for var in config.VARIABLES
    }
    flags = np.array([d in preliminary_days for d in range(1, ndays + 1)])
    return xr.Dataset(
        data_vars,
        coords={
            "time": times,
            "zlev": [0.0],
            "lat": np.arange(4.0),
            "lon": np.arange(4.0),
            "preliminary": ("time", flags),
        },
    )


@pytest.fixture
def repo(tmp_path: Path) -> icechunk.Repository:
    target = store.StoreTarget(local_path=tmp_path / "store")
    return store.open_or_create_repo(target)


def _write_daily(repo: icechunk.Repository, ds: xr.Dataset) -> None:
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(ds, session, group=config.DAILY_GROUP)
    session.commit("synthetic daily data")


def test_preliminary_coordinate_round_trip(repo: icechunk.Repository) -> None:
    _write_daily(repo, _synthetic_daily(2024, 1, preliminary_days={30, 31}))

    session = repo.readonly_session("main")
    ds = store.open_group(session, config.DAILY_GROUP)
    assert ds is not None
    assert "preliminary" in ds.coords
    assert ds["preliminary"].dims == ("time",)
    assert ds["preliminary"].dtype == np.bool_

    final_only = ds.set_xindex("preliminary").sel(preliminary=False)
    assert final_only.sizes["time"] == 29


def test_store_days_state_reads_flags(repo: icechunk.Repository) -> None:
    _write_daily(repo, _synthetic_daily(2024, 1, preliminary_days={31}))
    state = store_days_state(repo.readonly_session("main"))
    assert len(state) == 31
    assert bool(state[date(2024, 1, 31)])
    assert not bool(state[date(2024, 1, 1)])


def test_flag_flip_in_place(repo: icechunk.Repository) -> None:
    _write_daily(repo, _synthetic_daily(2024, 1, preliminary_days={31}))

    session = repo.writable_session("main")
    group = zarr.open_group(session.store, path=config.DAILY_GROUP, mode="a")
    prelim = group["preliminary"]
    assert isinstance(prelim, zarr.Array)
    prelim[30] = False
    session.commit("flip")

    state = store_days_state(repo.readonly_session("main"))
    assert not any(state.values())
    times = store.group_times(repo.readonly_session("main"), config.DAILY_GROUP)
    assert times is not None
    assert len(times) == 31


def test_rollup_refuses_preliminary_month(repo: icechunk.Repository) -> None:
    _write_daily(repo, _synthetic_daily(2024, 1, preliminary_days={31}))
    with pytest.raises(RuntimeError, match="preliminary"):
        rollup.rollup_month(repo, date(2024, 1, 1))


def test_rollup_refuses_incomplete_month(repo: icechunk.Repository) -> None:
    ds = _synthetic_daily(2024, 1, preliminary_days=set()).isel(time=slice(0, 20))
    _write_daily(repo, ds)
    with pytest.raises(RuntimeError, match="20 of 31"):
        rollup.rollup_month(repo, date(2024, 1, 1))


def test_rollup_writes_correct_values_chunks_and_encoding(
    repo: icechunk.Repository,
) -> None:
    ds = _synthetic_daily(2024, 1, preliminary_days=set())
    _write_daily(repo, ds)
    rollup.rollup_month(repo, date(2024, 1, 1))

    session = repo.readonly_session("main")
    monthly = store.open_group(session, config.MONTHLY_GROUP)
    assert monthly is not None
    assert set(monthly.data_vars) == set(config.monthly_variable_names())
    assert monthly.sizes["time"] == 1

    # values match a direct xarray computation, within int16 scale rounding
    expected = ds["sst"].mean("time")
    actual = monthly["sst_mean"].isel(time=0)
    assert np.allclose(actual.values, expected.values, atol=0.006)

    err_expected = ds["err"].std("time")
    err_actual = monthly["err_std"].isel(time=0)
    assert np.allclose(err_actual.values, err_expected.values, atol=0.0006)

    # chunk shape and dtype on the stored arrays
    group = zarr.open_group(session.store, path=config.MONTHLY_GROUP, mode="r")
    sst_mean = group["sst_mean"]
    assert isinstance(sst_mean, zarr.Array)
    assert sst_mean.chunks == config.MONTHLY_CHUNKS
    assert sst_mean.dtype == np.int16


def test_rollup_catch_up_is_idempotent(repo: icechunk.Repository) -> None:
    _write_daily(repo, _synthetic_daily(2024, 1, preliminary_days=set()))
    session = repo.readonly_session("main")
    state = store_days_state(session)

    ready = rollup.months_ready(state, store.group_times(session, config.MONTHLY_GROUP))
    assert ready == [date(2024, 1, 1)]
    rollup.rollup_month(repo, ready[0])

    session = repo.readonly_session("main")
    again = rollup.months_ready(state, store.group_times(session, config.MONTHLY_GROUP))
    assert again == []


def test_store_target_requires_exactly_one_destination() -> None:
    with pytest.raises(ValueError, match="Exactly one"):
        store.StoreTarget()
    with pytest.raises(ValueError, match="Exactly one"):
        store.StoreTarget(local_path=Path("/x"), s3_bucket="b")


def test_store_target_s3_acl_defaults_and_can_be_disabled() -> None:
    """Every S3 write gets ``bucket-owner-full-control`` unless disabled.

    Construction only (no network): the icechunk ``Storage`` type doesn't
    expose its headers for inspection, so this just checks both configurations
    build without error and that the default value is the expected ACL.
    """
    default_target = store.StoreTarget(s3_bucket="b")
    assert default_target.s3_acl == "bucket-owner-full-control"
    default_target.storage()

    store.StoreTarget(s3_bucket="b", s3_acl=None).storage()


def test_repo_config_registers_noaa_container() -> None:
    repo_config = store.build_virtual_chunk_container_config()
    container = repo_config.get_virtual_chunk_container(
        "s3://noaa-cdr-sea-surface-temp-optimum-interpolation-pds/"
    )
    assert container is not None
