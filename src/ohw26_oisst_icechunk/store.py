"""Icechunk repo plumbing: storage targets, virtual chunk container, state helpers.

Every CLI command takes a *storage target* — a local path, an S3 bucket/prefix,
or (post-Stage 2) an Arraylake repo — so the same commands run on a laptop
against a directory and in Actions against the published store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path  # noqa: TC003 - dataclass field type
from typing import Any

import icechunk
import numpy as np
import xarray as xr

from ohw26_oisst_icechunk import sources

# --- Storage targets ---------------------------------------------------------


@dataclass(frozen=True)
class StoreTarget:
    """Where the Icechunk repo lives. Exactly one destination is set."""

    local_path: Path | None = None
    s3_bucket: str | None = None
    s3_prefix: str = "oisst"
    s3_region: str = "us-east-1"
    s3_endpoint: str | None = None
    s3_anonymous: bool = False
    s3_acl: str | None = "bucket-owner-full-control"
    arraylake_repo: str | None = None

    def __post_init__(self) -> None:
        """Validate that exactly one destination is configured."""
        destinations = [
            self.local_path is not None,
            self.s3_bucket is not None,
            self.arraylake_repo is not None,
        ]
        if sum(destinations) != 1:
            msg = "Exactly one of local_path, s3_bucket, or arraylake_repo must be set"
            raise ValueError(msg)

    def storage(self) -> icechunk.Storage:
        """Build the icechunk ``Storage`` for this target.

        Every S3 write carries an ``x-amz-acl: bucket-owner-full-control``
        header (via Icechunk's ``write_headers``) unless ``s3_acl`` is
        ``None``. AWS accepts this canned ACL under every Object Ownership
        mode - including "bucket owner enforced", where ACLs are otherwise
        ignored - so it's a safe no-op for same-account writes and only
        matters for cross-account destinations (e.g. Source.coop). Set
        ``s3_acl=None`` for non-AWS endpoints that reject the header.
        """
        if self.local_path is not None:
            return icechunk.local_filesystem_storage(str(self.local_path))
        if self.s3_bucket is not None:
            return icechunk.s3_storage(
                bucket=self.s3_bucket,
                prefix=self.s3_prefix,
                region=self.s3_region,
                endpoint_url=self.s3_endpoint,
                anonymous=self.s3_anonymous or None,
                from_env=not self.s3_anonymous,
                allow_http=(self.s3_endpoint or "").startswith("http://"),
                force_path_style=bool(self.s3_endpoint),
                write_headers={"x-amz-acl": self.s3_acl} if self.s3_acl else None,
            )
        msg = "Arraylake targets await the Stage 2 destination decision (see PLAN.md)"
        raise NotImplementedError(msg)


# --- Virtual chunk container / credentials -----------------------------------


def build_virtual_chunk_container_config() -> icechunk.RepositoryConfig:
    """``RepositoryConfig`` registering the anonymous NOAA S3 bucket as a
    virtual chunk container."""
    config = icechunk.RepositoryConfig.default()
    config.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(
            url_prefix=sources.URL_PREFIX,
            store=icechunk.s3_store(region=sources.REGION, anonymous=True),
        ),
    )
    return config


def virtual_chunk_credentials() -> Any:
    """Anonymous credentials authorizing access to the NOAA virtual chunks."""
    return icechunk.containers_credentials(
        {sources.URL_PREFIX: icechunk.s3_anonymous_credentials()},
    )


def open_or_create_repo(target: StoreTarget) -> icechunk.Repository:
    """Open (or create on first run) the repo, with the NOAA virtual chunk
    container registered and authorized.

    On creation the config is persisted with ``save_config()`` (Stage 0
    decision) so readers inherit the container registration from the store.
    """
    storage = target.storage()
    creating = not icechunk.Repository.exists(storage)
    repo = icechunk.Repository.open_or_create(
        storage,
        config=build_virtual_chunk_container_config(),
        authorize_virtual_chunk_access=virtual_chunk_credentials(),
    )
    if creating:
        repo.save_config()
    return repo


# --- Store state helpers -----------------------------------------------------


def open_group(session: icechunk.Session, group: str) -> xr.Dataset | None:
    """Open a store group as an xarray Dataset, or ``None`` if it doesn't exist."""
    try:
        return xr.open_zarr(
            session.store, group=group, consolidated=False, zarr_format=3
        )
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


def group_times(session: icechunk.Session, group: str) -> np.ndarray | None:
    """The ``time`` coordinate values already in a store group.

    Returns ``None`` if the group (or its ``time`` coordinate) doesn't exist
    yet, e.g. on the very first write.
    """
    ds = open_group(session, group)
    if ds is None or "time" not in ds:
        return None
    return np.asarray(ds["time"].values)


def naive_utc(dt: datetime) -> datetime:
    """Convert a tz-aware datetime to naive UTC (pass through if already naive)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def times_to_days(times: np.ndarray) -> list[date]:
    """Truncate a datetime64 time coordinate to calendar days.

    OISST daily timestamps sit at 12:00Z rather than midnight.
    """
    days = times.astype("datetime64[D]")
    return [d.astype(object) for d in days]


def day_in_times(times: np.ndarray | None, d: date) -> bool:
    """Whether calendar day ``d`` already has a timestamp in ``times``."""
    if times is None:
        return False
    return bool(np.any(times.astype("datetime64[D]") == np.datetime64(d)))


def month_in_times(times: np.ndarray | None, month: date) -> bool:
    """Whether the calendar month of ``month`` already has a timestamp in ``times``."""
    if times is None:
        return False
    return bool(np.any(times.astype("datetime64[M]") == np.datetime64(month, "M")))
