"""NOAA source layout: key construction, date parsing, and bucket listing.

The source data lives in the NOAA Open Data (anonymous S3) bucket::

    s3://noaa-cdr-sea-surface-temp-optimum-interpolation-pds/
        data/v2.1/avhrr/<YYYYMM>/oisst-avhrr-v02r01.<YYYYMMDD>.nc            (final)
        data/v2.1/avhrr/<YYYYMM>/oisst-avhrr-v02r01.<YYYYMMDD>_preliminary.nc (preliminary)

Preliminary files are published almost immediately and replaced by the final
file roughly two weeks later (NOAA deletes the preliminary object when the
final lands). Settled months contain only final files.

Everything here is deliberately free of icechunk / S3 side effects so it can be
unit tested with a fake filesystem object.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Protocol

# --- Source layout -----------------------------------------------------------

BUCKET = "noaa-cdr-sea-surface-temp-optimum-interpolation-pds"
URL_PREFIX = f"s3://{BUCKET}/"
DATA_PREFIX = "data/v2.1/avhrr"
REGION = "us-east-1"

# obstore wants the prefix without a trailing slash for the registry key, while
# icechunk's VirtualChunkContainer matches against the trailing-slash form.
STORE_PREFIX = URL_PREFIX.rstrip("/")

_FINAL_RE = re.compile(r"^oisst-avhrr-v02r01\.(\d{8})\.nc$")
_PRELIM_RE = re.compile(r"^oisst-avhrr-v02r01\.(\d{8})_preliminary\.nc$")


class ListableFilesystem(Protocol):
    """The one fsspec method the listing helpers need."""

    def ls(self, path: str, detail: bool = ...) -> list[Any]:
        """List a directory's entries."""


# --- Keys / URLs -------------------------------------------------------------


def key_for_date(d: date, preliminary: bool = False) -> str:
    """Build the exact S3 object key (no bucket, no scheme) for a given date."""
    suffix = "_preliminary" if preliminary else ""
    return f"{DATA_PREFIX}/{d:%Y%m}/oisst-avhrr-v02r01.{d:%Y%m%d}{suffix}.nc"


def url_for_date(d: date, preliminary: bool = False) -> str:
    """Build the full ``s3://`` URL for a given date."""
    return f"{URL_PREFIX}{key_for_date(d, preliminary)}"


# --- Date parsing / listing --------------------------------------------------


def parse_date_from_key(key: str, preliminary: bool = False) -> date | None:
    """Parse the date out of a key's filename.

    Returns ``None`` when the filename does not match the requested flavor, so
    final parsing skips ``_preliminary`` files and vice versa.
    """
    name = key.rstrip("/").split("/")[-1]
    regex = _PRELIM_RE if preliminary else _FINAL_RE
    match = regex.match(name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()  # noqa: DTZ007


def dates_from_keys(keys: list[str], preliminary: bool = False) -> list[date]:
    """Parse and sort the dates of the requested flavor from a list of keys."""
    parsed = (parse_date_from_key(k, preliminary) for k in keys)
    return sorted(d for d in parsed if d is not None)


def _list(fs: ListableFilesystem, path: str) -> list[str]:
    """List a directory, tolerating a missing prefix (returns empty)."""
    try:
        return list(fs.ls(path, detail=False))
    except FileNotFoundError:
        return []


def dates_available(
    fs: ListableFilesystem,
    preliminary: bool = False,
    months: list[str] | None = None,
) -> list[date]:
    """List available dates for the requested flavor.

    ``fs`` is any fsspec-like object exposing ``ls(path, detail=False)`` (in
    production an anonymous ``s3fs.S3FileSystem``). ``months`` optionally
    restricts the scan to specific ``YYYYMM`` prefixes; when omitted every
    month directory under ``data/v2.1/avhrr/`` is discovered and scanned.
    """
    if months is None:
        base = f"{BUCKET}/{DATA_PREFIX}/"
        months = [entry.rstrip("/").split("/")[-1] for entry in _list(fs, base)]

    keys: list[str] = []
    for month in months:
        keys.extend(_list(fs, f"{BUCKET}/{DATA_PREFIX}/{month}/"))

    return dates_from_keys(keys, preliminary)


def recent_months(now: datetime, count: int = 2) -> list[str]:
    """Return the ``YYYYMM`` prefixes for the current and preceding months."""
    months: list[str] = []
    year, month = now.year, now.month
    for _ in range(count):
        months.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


def months_between(start: date, end: date) -> list[str]:
    """All ``YYYYMM`` prefixes from ``start``'s month through ``end``'s month."""
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def anon_s3() -> Any:
    """Anonymous S3 filesystem for listing the public NOAA bucket.

    The endpoint and region are pinned explicitly so that ambient AWS profile
    config (e.g. an ``AWS_PROFILE`` pointing at a Source.coop profile with its
    own ``endpoint_url``) cannot redirect this public NOAA listing elsewhere.
    """
    import s3fs  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    return s3fs.S3FileSystem(
        anon=True,
        endpoint_url=f"https://s3.{REGION}.amazonaws.com",
        client_kwargs={"region_name": REGION},
    )
