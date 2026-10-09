"""Store layout constants: groups, grid, chunk shapes, and encoding.

The numbers here are the Stage 0 decisions from ``Stage_0.md`` / ``PLAN.md`` §2:
monthly statistics are ``int16`` with per-variable ``scale_factor`` (0.01 for
``sst_*``/``anom_*``, 0.001 for ``err_*``/``ice_*``), ``_FillValue=-32768``,
compressed with blosc-zstd level 3 + shuffle, chunked ``(24, 1, 90, 90)``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from zarr.codecs import BloscCodec, BloscShuffle

if TYPE_CHECKING:
    from collections.abc import Mapping

# --- Groups ------------------------------------------------------------------

DAILY_GROUP = "daily"
MONTHLY_GROUP = "monthly"

# --- Source dataset ----------------------------------------------------------

# First day of OISST v2.1 data.
START_DATE = date(1981, 9, 1)

# The data variables in daily files.
VARIABLES = ("sst", "anom", "err", "ice")

# Chunk shape of the `time` coordinate in both `daily/` and `monthly/`. One
# chunk per timestep makes opening the group read thousands of tiny chunks
# (~1.3 s per 16k, even when inlined in the manifest); 4096 keeps it to a few.
TIME_CHUNKS = (4096,)

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


# --- monthly/ metadata --------------------------------------------------------

# What each source variable is, as a noun phrase for a statistic's long_name.
_DAILY_BASE = {
    "sst": "daily sea surface temperature",
    "anom": "daily sea surface temperature anomaly",
    "err": "daily estimated error standard deviation of sea surface temperature",
    "ice": "daily sea ice concentration",
}
_STAT_WORD = {
    "min": "minimum",
    "max": "maximum",
    "mean": "mean",
    "std": "standard deviation",
}
_CELL_METHOD = {
    "min": "minimum",
    "max": "maximum",
    "mean": "mean",
    "std": "standard_deviation",
}

# Daily group attrs worth carrying over to monthly/ (provenance and extent).
_INHERITED_GROUP_ATTRS = ("references", "platform", "sensor")
_INHERITED_GROUP_ATTR_PREFIXES = ("geospatial_",)


def monthly_variable_attrs(name: str) -> dict[str, str]:
    """CF attrs for a monthly statistic variable such as ``ice_max``.

    Deliberately omits ``valid_min``/``valid_max`` (the daily values describe
    the wrong units once a ``scale_factor`` is applied) and the encoding attrs
    ``scale_factor``/``_FillValue``, which come from :func:`monthly_encoding`.
    """
    var, stat = name.split("_", 1)
    return {
        "long_name": f"Monthly {_STAT_WORD[stat]} of {_DAILY_BASE[var]}",
        # ice is stored by NOAA as a 0-1 fraction despite its "%" units attr.
        "units": "1" if var == "ice" else "Celsius",
        "cell_methods": f"time: {_CELL_METHOD[stat]}",
    }


def monthly_group_attrs(daily_attrs: Mapping[str, Any]) -> dict[str, Any]:
    """Global attrs for the ``monthly/`` group.

    Describes what this project computes, and copies provenance and
    geospatial attrs from the daily group when present.
    """
    attrs: dict[str, Any] = {
        "title": "NOAA Optimum Interpolation SST v2.1 monthly statistics",
        "summary": (
            "Monthly minimum, maximum, mean and standard deviation of final "
            "daily NOAA OISST v2.1 fields, computed by this project from the "
            "daily/ group. Not a NOAA product."
        ),
        "Conventions": "CF-1.6, ACDD-1.3",
        "source": "NOAA OISST v2.1 daily AVHRR-only final files",
        "institution": "NERACOOS / Gulf of Maine Research Institute",
        "creator_name": "NERACOOS / GMRI",
        "history": (
            "Monthly statistics computed from the daily/ group of this store "
            "by ohw26_oisst_icechunk."
        ),
    }
    attrs |= {
        key: value
        for key, value in daily_attrs.items()
        if key in _INHERITED_GROUP_ATTRS
        or key.startswith(_INHERITED_GROUP_ATTR_PREFIXES)
    }
    return attrs


def monthly_compressors() -> list[BloscCodec]:
    """The compressor chain for monthly statistics (Stage 0 measurement winner)."""
    return [BloscCodec(cname="zstd", clevel=3, shuffle=BloscShuffle.shuffle)]


def monthly_encoding(names: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Zarr encoding for the monthly statistics variables.

    Also covers the ``time`` coordinate. Only applied on the group's first
    write; appends inherit it from the store.
    """
    if names is None:
        names = monthly_variable_names()
    encoding: dict[str, dict[str, Any]] = {
        name: {
            "dtype": "int16",
            "scale_factor": scale_factor_for(name),
            "_FillValue": MONTHLY_FILL_VALUE,
            "chunks": MONTHLY_CHUNKS,
            "compressors": monthly_compressors(),
        }
        for name in names
    }
    encoding["time"] = {"chunks": TIME_CHUNKS}
    return encoding
