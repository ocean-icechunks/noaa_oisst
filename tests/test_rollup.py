"""Unit tests for monthly statistics, readiness gating, and encoding config."""

from __future__ import annotations

import calendar
from datetime import date

import numpy as np
import pytest
import xarray as xr

from ohw26_oisst_icechunk import config
from ohw26_oisst_icechunk.rollup import monthly_statistics, months_ready

# --- monthly_statistics -------------------------------------------------------


def _synthetic_month() -> xr.Dataset:
    times = [np.datetime64(f"2024-01-0{d}T12:00:00") for d in (1, 2, 3)]
    data = np.array(
        [
            [[1.0, 2.0], [3.0, 4.0]],
            [[5.0, 6.0], [7.0, 8.0]],
            [[9.0, 10.0], [11.0, 12.0]],
        ],
    )
    return xr.Dataset(
        {"sst": (("time", "lat", "lon"), data)},
        coords={"time": times, "lat": [0, 1], "lon": [0, 1]},
    )


def test_monthly_statistics_produces_stat_variables() -> None:
    out = monthly_statistics(_synthetic_month(), date(2024, 1, 1))
    assert set(out.data_vars) == {"sst_min", "sst_max", "sst_mean", "sst_std"}


def test_monthly_statistics_single_time_step_at_month_start() -> None:
    out = monthly_statistics(_synthetic_month(), date(2024, 1, 1))
    assert out.sizes["time"] == 1
    assert out["time"].values[0] == np.datetime64("2024-01-01")


def test_monthly_statistics_computes_correct_reductions() -> None:
    out = monthly_statistics(_synthetic_month(), date(2024, 1, 1))
    cell = {"time": 0, "lat": 0, "lon": 0}
    assert out["sst_min"].isel(cell).item() == 1.0
    assert out["sst_max"].isel(cell).item() == 9.0
    assert out["sst_mean"].isel(cell).item() == 5.0
    assert out["sst_std"].isel(cell).item() == pytest.approx(np.std([1.0, 5.0, 9.0]))


def test_monthly_statistics_preserves_float32() -> None:
    ds = _synthetic_month().astype(np.float32)
    out = monthly_statistics(ds, date(2024, 1, 1))
    assert all(out[v].dtype == np.float32 for v in out.data_vars)


# --- months_ready -------------------------------------------------------------


def _full_month(year: int, month: int, preliminary: bool = False) -> dict[date, bool]:
    days = calendar.monthrange(year, month)[1]
    return {date(year, month, d): preliminary for d in range(1, days + 1)}


def test_complete_final_month_is_ready() -> None:
    assert months_ready(_full_month(2024, 1), None) == [date(2024, 1, 1)]


def test_incomplete_month_is_not_ready() -> None:
    store_days = _full_month(2024, 1)
    del store_days[date(2024, 1, 15)]
    assert months_ready(store_days, None) == []


def test_month_with_any_preliminary_day_is_not_ready() -> None:
    store_days = _full_month(2024, 1)
    store_days[date(2024, 1, 31)] = True
    assert months_ready(store_days, None) == []


def test_month_already_in_monthly_group_is_not_ready() -> None:
    monthly_times = np.array(["2024-01-01"], dtype="datetime64[ns]")
    assert months_ready(_full_month(2024, 1), monthly_times) == []


def test_ready_months_are_sorted() -> None:
    store_days = {**_full_month(2024, 2), **_full_month(2024, 1)}
    assert months_ready(store_days, None) == [date(2024, 1, 1), date(2024, 2, 1)]


# --- encoding config ----------------------------------------------------------


def test_monthly_variable_names_are_the_16_statistics() -> None:
    names = config.monthly_variable_names()
    assert len(names) == 16
    assert "sst_min" in names
    assert "ice_std" in names


def test_scale_factors_per_variable() -> None:
    assert config.scale_factor_for("sst_mean") == 0.01
    assert config.scale_factor_for("anom_std") == 0.01
    assert config.scale_factor_for("err_std") == 0.001
    assert config.scale_factor_for("ice_min") == 0.001


def test_monthly_encoding_shape_and_dtype() -> None:
    enc = config.monthly_encoding()
    assert set(enc) == set(config.monthly_variable_names())
    for name, spec in enc.items():
        assert spec["chunks"] == (24, 1, 90, 90)
        assert spec["dtype"] == "int16"
        assert spec["_FillValue"] == -32768
        assert spec["scale_factor"] == config.scale_factor_for(name)
