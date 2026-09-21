"""Snapshot expiry + garbage collection.

Do not enable in automation until the GC-vs-virtual-refs check in TASKS.md has been
run against pinned icechunk 2.0.x.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import icechunk

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 35


def retention_cutoff(now: datetime, days: int = DEFAULT_RETENTION_DAYS) -> datetime:
    """Cutoff datetime for snapshot expiry / GC."""
    return now - timedelta(days=days)


def expire(
    repo: icechunk.Repository,
    days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> Any:
    """Expire snapshots older than ``days`` and garbage-collect.

    icechunk 2.0.x API note: ``expire_snapshots(older_than=...)`` has no
    ``dry_run`` argument (it only rewrites branch history), so under a dry run
    we skip expiry and use ``garbage_collect(..., dry_run=True)`` to report.
    """
    cutoff = retention_cutoff(datetime.now(tz=UTC), days=days)
    logger.info("Retention cutoff: %s (dry_run=%s)", cutoff.isoformat(), dry_run)

    if dry_run:
        summary = repo.garbage_collect(cutoff, dry_run=True)
        logger.warning("Dry run, would garbage collect: %s", summary)
        return summary

    expired = repo.expire_snapshots(older_than=cutoff)
    logger.info("Expired %d snapshots: %s", len(expired), expired)
    summary = repo.garbage_collect(cutoff)
    logger.info("Garbage collection summary: %s", summary)
    return summary
