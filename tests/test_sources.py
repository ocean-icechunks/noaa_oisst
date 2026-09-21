"""Unit tests for NOAA source-layout helpers (ported from the Dagster repo).

These tests never touch real S3 - the listing helpers take an injectable
fsspec-like filesystem object, and here we pass a small in-memory fake.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from ohw26_oisst_icechunk import sources

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class FakeS3:
    """Minimal fsspec-like filesystem backed by an in-memory tree."""

    def __init__(self, tree: dict[str, list[str]]):
        self.tree = {k.rstrip("/") + "/": v for k, v in tree.items()}

    def ls(self, path: str, detail: bool = False) -> list[str]:  # noqa: ARG002
        norm = path.rstrip("/") + "/"
        if norm not in self.tree:
            raise FileNotFoundError(path)
        return self.tree[norm]


def test_key_for_date_final() -> None:
    key = sources.key_for_date(date(2024, 1, 5), preliminary=False)
    assert key == "data/v2.1/avhrr/202401/oisst-avhrr-v02r01.20240105.nc"


def test_key_for_date_preliminary() -> None:
    key = sources.key_for_date(date(2026, 7, 3), preliminary=True)
    assert key == "data/v2.1/avhrr/202607/oisst-avhrr-v02r01.20260703_preliminary.nc"


def test_url_for_date_final() -> None:
    url = sources.url_for_date(date(2024, 1, 5), preliminary=False)
    assert url == (
        "s3://noaa-cdr-sea-surface-temp-optimum-interpolation-pds/"
        "data/v2.1/avhrr/202401/oisst-avhrr-v02r01.20240105.nc"
    )


def test_parse_date_from_key_final() -> None:
    key = "bucket/data/v2.1/avhrr/202401/oisst-avhrr-v02r01.20240105.nc"
    assert sources.parse_date_from_key(key, preliminary=False) == date(2024, 1, 5)


def test_parse_date_from_key_final_ignores_preliminary() -> None:
    key = "bucket/data/v2.1/avhrr/202607/oisst-avhrr-v02r01.20260703_preliminary.nc"
    assert sources.parse_date_from_key(key, preliminary=False) is None


def test_parse_date_from_key_preliminary() -> None:
    key = "bucket/data/v2.1/avhrr/202607/oisst-avhrr-v02r01.20260703_preliminary.nc"
    assert sources.parse_date_from_key(key, preliminary=True) == date(2026, 7, 3)


def test_parse_date_from_key_preliminary_ignores_final() -> None:
    key = "bucket/data/v2.1/avhrr/202401/oisst-avhrr-v02r01.20240105.nc"
    assert sources.parse_date_from_key(key, preliminary=True) is None


def test_dates_from_keys_sorts_and_filters() -> None:
    keys = [
        "b/oisst-avhrr-v02r01.20240103.nc",
        "b/oisst-avhrr-v02r01.20240101.nc",
        "b/oisst-avhrr-v02r01.20240102_preliminary.nc",
        "b/some-other-file.txt",
    ]
    assert sources.dates_from_keys(keys, preliminary=False) == [
        date(2024, 1, 1),
        date(2024, 1, 3),
    ]
    assert sources.dates_from_keys(keys, preliminary=True) == [date(2024, 1, 2)]


def _month_dir(month: str) -> str:
    return f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/{month}/"


def test_dates_available_for_given_months() -> None:
    fs = FakeS3(
        {
            _month_dir("202401"): [
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/202401/oisst-avhrr-v02r01.20240101.nc",
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/202401/oisst-avhrr-v02r01.20240102.nc",
            ],
            _month_dir("202607"): [
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/202607/oisst-avhrr-v02r01.20260701.nc",
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/202607/oisst-avhrr-v02r01.20260702_preliminary.nc",
            ],
        },
    )
    finals = sources.dates_available(fs, preliminary=False, months=["202401", "202607"])
    assert finals == [date(2024, 1, 1), date(2024, 1, 2), date(2026, 7, 1)]

    prelims = sources.dates_available(fs, preliminary=True, months=["202401", "202607"])
    assert prelims == [date(2026, 7, 2)]


def test_dates_available_missing_month_is_skipped() -> None:
    fs = FakeS3({_month_dir("202401"): []})
    assert (
        sources.dates_available(fs, preliminary=False, months=["202401", "209912"])
        == []
    )


def test_dates_available_discovers_months_when_not_given() -> None:
    base = f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/"
    fs = FakeS3(
        {
            base: [
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/198109",
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/202401",
            ],
            _month_dir("198109"): [
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/198109/oisst-avhrr-v02r01.19810901.nc",
            ],
            _month_dir("202401"): [
                f"{sources.NODD_BUCKET}/{sources.NODD_DATA_PREFIX}/202401/oisst-avhrr-v02r01.20240101.nc",
            ],
        },
    )
    assert sources.dates_available(fs, preliminary=False) == [
        date(1981, 9, 1),
        date(2024, 1, 1),
    ]


def test_recent_months_wraps_years() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    assert sources.recent_months(now, count=3) == ["202601", "202512", "202511"]


def test_anon_s3_ignores_ambient_profile_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config"
    config_path.write_text("[profile fake]\nendpoint_url = https://example.invalid\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("AWS_PROFILE", "fake")

    fs = sources.anon_s3()

    assert fs.s3.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"


def test_months_between() -> None:
    assert sources.months_between(date(2025, 11, 5), date(2026, 2, 1)) == [
        "202511",
        "202512",
        "202601",
        "202602",
    ]
    assert sources.months_between(date(2026, 2, 1), date(2026, 2, 28)) == ["202602"]
