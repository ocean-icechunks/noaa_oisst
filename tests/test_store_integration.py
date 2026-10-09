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
from typing import Any

import icechunk
import icechunk.xarray
import numpy as np
import pytest
import xarray as xr
import zarr

from ohw26_oisst_icechunk import config, repair, rollup, store
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
    return store.open_repo(target, create=True)


def _write_daily(repo: icechunk.Repository, ds: xr.Dataset) -> None:
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(ds, session, group=config.DAILY_GROUP)
    session.commit("synthetic daily data")


def _append_daily(repo: icechunk.Repository, ds: xr.Dataset) -> None:
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(
        ds, session, group=config.DAILY_GROUP, append_dim="time"
    )
    session.commit("synthetic daily append")


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


def test_rollup_writes_time_coordinate_in_few_chunks(
    repo: icechunk.Repository,
) -> None:
    _write_daily(repo, _synthetic_daily(2024, 1, preliminary_days=set()))
    rollup.rollup_month(repo, date(2024, 1, 1))

    session = repo.readonly_session("main")
    group = zarr.open_group(session.store, path=config.MONTHLY_GROUP, mode="r")
    time = group["time"]
    assert isinstance(time, zarr.Array)
    assert time.chunks == config.TIME_CHUNKS


def test_rollup_writes_monthly_attrs_and_appends_keep_them(
    repo: icechunk.Repository,
) -> None:
    daily_ds = _synthetic_daily(2024, 1, preliminary_days=set())
    daily_ds.attrs = {"references": "https://example.org/ref", "title": "daily"}
    daily_ds["ice"].attrs = {"units": "%", "valid_min": 0, "valid_max": 100}
    _write_daily(repo, daily_ds)
    rollup.rollup_month(repo, date(2024, 1, 1))

    def monthly_group() -> zarr.Group:
        session = repo.readonly_session("main")
        return zarr.open_group(session.store, path=config.MONTHLY_GROUP, mode="r")

    group = monthly_group()
    assert group.attrs["Conventions"] == "CF-1.6, ACDD-1.3"
    assert group.attrs["references"] == "https://example.org/ref"
    ice_max = dict(group["ice_max"].attrs)
    assert ice_max["long_name"] == "Monthly maximum of daily sea ice concentration"
    assert ice_max["units"] == "1"
    assert "valid_max" not in ice_max
    assert ice_max["scale_factor"] == 0.001  # encoding attrs still present
    assert ice_max["_FillValue"] == config.MONTHLY_FILL_VALUE

    # A subsequent append (February) leaves the variable attrs as written. The
    # group attrs are rewritten by xarray's append from the new month's ds,
    # which is consistent as long as the daily group attrs are.
    feb = _synthetic_daily(2024, 2, preliminary_days=set())
    feb.attrs = daily_ds.attrs
    _append_daily(repo, feb)
    rollup.rollup_month(repo, date(2024, 2, 1))
    group = monthly_group()
    assert dict(group["ice_max"].attrs) == ice_max
    assert dict(group.attrs)["references"] == "https://example.org/ref"


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


def test_open_group_returns_none_for_missing_group(
    repo: icechunk.Repository,
) -> None:
    session = repo.readonly_session("main")
    assert store.open_group(session, config.DAILY_GROUP) is None


class _RaisingSession:
    """Stub session whose ``.store`` raises a non-"group missing" error."""

    @property
    def store(self) -> object:
        msg = "permission denied"
        raise PermissionError(msg)


def test_open_group_propagates_non_group_not_found_errors() -> None:
    with pytest.raises(PermissionError, match="permission denied"):
        store.open_group(_RaisingSession(), config.DAILY_GROUP)  # type: ignore[arg-type]


def test_open_repo_without_create_raises_and_leaves_no_directory(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist"
    target = store.StoreTarget(local_path=missing)

    with pytest.raises(FileNotFoundError, match="backfill-daily --create"):
        store.open_repo(target)

    assert not missing.exists()


def test_open_repo_with_create_creates_then_default_call_opens_it(
    tmp_path: Path,
) -> None:
    target = store.StoreTarget(local_path=tmp_path / "store")

    created = store.open_repo(target, create=True)
    assert created is not None

    opened = store.open_repo(target)
    assert opened is not None


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


# --- repair_layout ------------------------------------------------------------

DAILY_TIME_ENCODING = {
    "chunks": (1,),
    "dtype": "float32",
    "units": "days since 1978-01-01T12:00:00",
}


def _legacy_store(repo: icechunk.Repository) -> xr.Dataset:
    """A store laid out as before the fix: (1,)-chunked time, daily-style attrs."""
    daily_ds = _synthetic_daily(2024, 1, preliminary_days=set())
    daily_ds.attrs = {"references": "https://example.org/ref", "title": "daily"}
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(
        daily_ds,
        session,
        group=config.DAILY_GROUP,
        encoding={"time": DAILY_TIME_ENCODING},
    )
    session.commit("legacy daily")

    monthly = rollup.monthly_statistics(daily_ds, date(2024, 1, 1)).load()
    # undo the fixes to mimic a legacy monthly group
    for name in monthly.data_vars:
        monthly[name].attrs = {
            "long_name": "Daily sea ice concentration",
            "units": "%",
            "valid_min": 0,
            "valid_max": 100,
        }
    monthly.attrs = {}
    encoding = config.monthly_encoding([str(n) for n in monthly.data_vars])
    encoding["time"] = {"chunks": (1,)}
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(
        monthly, session, group=config.MONTHLY_GROUP, encoding=encoding
    )
    session.commit("legacy monthly")
    return daily_ds


def _array(repo: icechunk.Repository, group: str, name: str) -> zarr.Array[Any]:
    session = repo.readonly_session("main")
    arr = zarr.open_group(session.store, path=group, mode="r")[name]
    assert isinstance(arr, zarr.Array)
    return arr


def _snapshot_count(repo: icechunk.Repository) -> int:
    return len(list(repo.ancestry(branch="main")))


def test_repair_layout_rechunks_time_and_fixes_monthly_attrs(
    repo: icechunk.Repository,
) -> None:
    daily_ds = _legacy_store(repo)
    before = _array(repo, config.DAILY_GROUP, "time")
    assert before.chunks == (1,)
    before_values, before_attrs = before[:], dict(before.attrs)
    before_fill = before.fill_value
    before_meta = (
        before.dtype,
        before.filters,
        before.compressors,
        before.serializer,
        getattr(before.metadata, "dimension_names", None),
    )
    before_decoded = store.group_times(
        repo.readonly_session("main"), config.DAILY_GROUP
    )
    monthly_before = _array(repo, config.MONTHLY_GROUP, "time")[:]
    sst_before = _array(repo, config.DAILY_GROUP, "sst")[:]
    prelim_before = _array(repo, config.DAILY_GROUP, "preliminary")[:]

    changes = repair.repair_layout(repo, dry_run=False)
    assert changes

    after = _array(repo, config.DAILY_GROUP, "time")
    assert after.chunks == config.TIME_CHUNKS
    assert np.array_equal(after[:], before_values)
    assert dict(after.attrs) == before_attrs
    assert np.array_equal(after.fill_value, before_fill, equal_nan=True)
    assert (
        after.dtype,
        after.filters,
        after.compressors,
        after.serializer,
        getattr(after.metadata, "dimension_names", None),
    ) == before_meta
    after_decoded = store.group_times(repo.readonly_session("main"), config.DAILY_GROUP)
    assert after_decoded is not None
    assert np.array_equal(after_decoded, before_decoded)  # type: ignore[arg-type]
    assert np.array_equal(
        after_decoded, daily_ds["time"].values.astype(after_decoded.dtype)
    )

    monthly_time = _array(repo, config.MONTHLY_GROUP, "time")
    assert monthly_time.chunks == config.TIME_CHUNKS
    assert np.array_equal(monthly_time[:], monthly_before)

    # untouched
    assert np.array_equal(_array(repo, config.DAILY_GROUP, "sst")[:], sst_before)
    assert np.array_equal(
        _array(repo, config.DAILY_GROUP, "preliminary")[:], prelim_before
    )

    ice_max = _array(repo, config.MONTHLY_GROUP, "ice_max")
    attrs = dict(ice_max.attrs)
    assert "valid_max" not in attrs
    assert "valid_min" not in attrs
    assert attrs["long_name"] == "Monthly maximum of daily sea ice concentration"
    assert attrs["units"] == "1"
    assert attrs["scale_factor"] == 0.001
    assert attrs["_FillValue"] == config.MONTHLY_FILL_VALUE
    group = zarr.open_group(
        repo.readonly_session("main").store, path=config.MONTHLY_GROUP, mode="r"
    )
    assert group.attrs["Conventions"] == "CF-1.6, ACDD-1.3"
    assert group.attrs["references"] == "https://example.org/ref"

    # the repaired monthly data still decodes with its scale factor
    monthly = store.open_group(repo.readonly_session("main"), config.MONTHLY_GROUP)
    assert monthly is not None
    expected = daily_ds["ice"].max("time").values
    assert np.allclose(monthly["ice_max"].isel(time=0).values, expected, atol=0.001)


def test_repair_layout_is_idempotent(repo: icechunk.Repository) -> None:
    _legacy_store(repo)
    repair.repair_layout(repo, dry_run=False)
    count = _snapshot_count(repo)
    assert repair.repair_layout(repo, dry_run=False) == []
    assert _snapshot_count(repo) == count


def test_repair_layout_dry_run_reports_but_does_not_commit(
    repo: icechunk.Repository,
) -> None:
    _legacy_store(repo)
    count = _snapshot_count(repo)
    changes = repair.repair_layout(repo, dry_run=True)
    assert changes
    assert _snapshot_count(repo) == count
    assert _array(repo, config.DAILY_GROUP, "time").chunks == (1,)


def test_repair_layout_on_empty_repo_is_a_noop(repo: icechunk.Repository) -> None:
    assert repair.repair_layout(repo, dry_run=False) == []


def test_repair_layout_skips_missing_monthly_group(repo: icechunk.Repository) -> None:
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(
        _synthetic_daily(2024, 1, preliminary_days=set()),
        session,
        group=config.DAILY_GROUP,
        encoding={"time": DAILY_TIME_ENCODING},
    )
    session.commit("daily only")
    changes = repair.repair_layout(repo, dry_run=False)
    assert len(changes) == 1
    assert _array(repo, config.DAILY_GROUP, "time").chunks == config.TIME_CHUNKS


def test_appends_after_repair_keep_the_time_chunk_grid(
    repo: icechunk.Repository,
) -> None:
    _legacy_store(repo)
    repair.repair_layout(repo, dry_run=False)

    feb = _synthetic_daily(2024, 2, preliminary_days=set())
    session = repo.writable_session("main")
    icechunk.xarray.to_icechunk(
        feb,
        session,
        group=config.DAILY_GROUP,
        append_dim="time",
    )
    session.commit("append february")
    assert _array(repo, config.DAILY_GROUP, "time").chunks == config.TIME_CHUNKS

    rollup.rollup_month(repo, date(2024, 2, 1))
    assert _array(repo, config.MONTHLY_GROUP, "time").chunks == config.TIME_CHUNKS
    assert _array(repo, config.MONTHLY_GROUP, "time").shape == (2,)


def _assert_monthly_attrs_clean(repo: icechunk.Repository) -> None:
    session = repo.readonly_session("main")
    monthly = zarr.open_group(session.store, path=config.MONTHLY_GROUP, mode="r")
    for name in config.monthly_variable_names():
        attrs = dict(monthly[name].attrs)
        assert "valid_min" not in attrs
        assert "valid_max" not in attrs
        for key, value in config.monthly_variable_attrs(name).items():
            assert attrs[key] == value
    assert monthly.attrs["Conventions"] == "CF-1.6, ACDD-1.3"
    assert monthly.attrs["references"] == "https://example.org/ref"


@pytest.mark.parametrize("path", ["append", "overwrite"])
def test_rollup_cleans_monthly_attrs_on_every_write_path(
    repo: icechunk.Repository, path: str
) -> None:
    jan = _legacy_store(repo)
    if path == "append":
        feb = _synthetic_daily(2024, 2, preliminary_days=set())
        # append_dim rewrites group attrs, so carry daily's (as NOAA files do)
        feb.attrs = jan.attrs
        _append_daily(repo, feb)
        rollup.rollup_month(repo, date(2024, 2, 1))
    else:
        rollup.rollup_month(repo, date(2024, 1, 1))
    _assert_monthly_attrs_clean(repo)
