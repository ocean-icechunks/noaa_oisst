"""Store layout constants: groups, grid, chunk shapes, and encoding.

The numbers here are the Stage 0 decisions from ``Stage_0.md`` / ``PLAN.md`` §2:
monthly statistics are ``int16`` with per-variable ``scale_factor`` (0.01 for
``sst_*``/``anom_*``, 0.001 for ``err_*``/``ice_*``), ``_FillValue=-32768``,
compressed with blosc-zstd level 3 + shuffle, chunked ``(24, 1, 90, 90)``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from zarr.codecs import BloscCodec, BloscShuffle

# --- Groups ------------------------------------------------------------------

DAILY_GROUP = "daily"
MONTHLY_GROUP = "monthly"

# --- Source dataset ----------------------------------------------------------

# First day of OISST v2.1 data.
START_DATE = date(1981, 9, 1)

# The data variables in daily files.
VARIABLES = ("sst", "anom", "err", "ice")

# Grid shape (lat, lon) of the 1/4-degree OISST grid.
GRID_SHAPE = (720, 1440)

# --- daily/ ------------------------------------------------------------------

# Chunk shape of the real (non-virtual) `preliminary` bool coordinate.
PRELIMINARY_CHUNKS = (1024,)

# --- monthly/ ----------------------------------------------------------------

# The statistics computed per source variable, over the days of each month.
STATISTICS = ("min", "max", "mean", "std")

# (time, zlev, lat, lon) chunk shape for every monthly statistic variable.
MONTHLY_CHUNKS = (24, 1, 90, 90)

MONTHLY_FILL_VALUE = -32768


def monthly_variable_names() -> list[str]:
    """The 16 monthly statistic variable names (``sst_min``, ``sst_max``, ...)."""
    return [f"{var}_{stat}" for var in VARIABLES for stat in STATISTICS]


def scale_factor_for(name: str) -> float:
    """Per-variable ``scale_factor`` (Stage 0 decision).

    ``err``/``ice`` statistics are small 0-1 fractions; at scale 0.01 most of
    their std values round to zero, so they get 0.001 instead.
    """
    return 0.001 if name.startswith(("err", "ice")) else 0.01


def monthly_compressors() -> list[BloscCodec]:
    """The compressor chain for monthly statistics (Stage 0 measurement winner)."""
    return [BloscCodec(cname="zstd", clevel=3, shuffle=BloscShuffle.shuffle)]


def monthly_encoding(names: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Zarr encoding for the monthly statistics variables.

    Only applied on the group's first write; appends inherit it from the store.
    """
    if names is None:
        names = monthly_variable_names()
    return {
        name: {
            "dtype": "int16",
            "scale_factor": scale_factor_for(name),
            "_FillValue": MONTHLY_FILL_VALUE,
            "chunks": MONTHLY_CHUNKS,
            "compressors": monthly_compressors(),
        }
        for name in names
    }
