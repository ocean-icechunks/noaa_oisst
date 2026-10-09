"""Idempotent layout and metadata repair for an existing store.

Stores written before issue #2 have a ``time`` coordinate with one chunk per
timestep in ``daily/`` and ``monthly/``, and ``monthly/`` variables carrying
inherited daily attrs. :func:`repair_layout` brings such a store in line with
:mod:`config` in one commit, and does nothing (no commit) when it already is.
Only materialized metadata and the ``time`` coordinate are touched; virtual
references and the data variables are left alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import zarr

from ohw26_oisst_icechunk import config

if TYPE_CHECKING:
    import icechunk

logger = logging.getLogger(__name__)

# Attrs that monthly statistics must not carry (see config.monthly_variable_attrs).
_STALE_MONTHLY_ATTRS = ("valid_min", "valid_max")


def rechunk_coordinate(group: zarr.Group, name: str, chunks: tuple[int, ...]) -> bool:
    """Rewrite ``group[name]`` with ``chunks``, preserving everything else.

    Values, dtype, fill value, codecs, dimension names and attrs are carried
    over unchanged. Returns ``False`` (touching nothing) when the array already
    has these chunks.
    """
    old = cast("zarr.Array[Any]", group[name])
    if tuple(old.chunks) == tuple(chunks):
        return False

    values = old[...]
    kwargs: dict[str, Any] = {
        "shape": old.shape,
        "chunks": chunks,
        "dtype": old.dtype,
        "fill_value": old.fill_value,
        "filters": old.filters,
        "compressors": old.compressors,
        "serializer": old.serializer,
        "dimension_names": getattr(old.metadata, "dimension_names", None),
        "attributes": dict(old.attrs),
    }
    del group[name]
    new = group.create_array(name, **kwargs)
    new[...] = values
    return True


def fix_monthly_metadata(monthly: zarr.Group, daily_attrs: dict[str, Any]) -> list[str]:
    """Replace inherited daily attrs on the monthly group and its statistics.

    ``scale_factor`` / ``_FillValue`` and any other attrs are kept. Returns a
    summary of what changed (one line for the variables, one for the group).
    """
    changes: list[str] = []
    fixed: list[str] = []
    for name in config.monthly_variable_names():
        if name not in monthly:
            continue
        arr = cast("zarr.Array[Any]", monthly[name])
        current = dict(arr.attrs)
        wanted = {
            k: v for k, v in current.items() if k not in _STALE_MONTHLY_ATTRS
        } | config.monthly_variable_attrs(name)
        if wanted != current:
            arr.attrs.put(wanted)
            fixed.append(name)
    if fixed:
        changes.append(f"monthly/: fixed attrs on {len(fixed)} variable(s)")

    group_wanted = config.monthly_group_attrs(daily_attrs)
    group_current = dict(monthly.attrs)
    if any(group_current.get(k) != v for k, v in group_wanted.items()):
        monthly.attrs.put(group_current | group_wanted)
        changes.append("monthly/: set group attrs")
    return changes


def _open_group(session: icechunk.Session, path: str) -> zarr.Group | None:
    try:
        return zarr.open_group(session.store, path=path, mode="r+")
    except (FileNotFoundError, zarr.errors.GroupNotFoundError):
        return None


def repair_layout(repo: icechunk.Repository, dry_run: bool = False) -> list[str]:
    """Bring ``time`` chunking and monthly metadata up to the current layout.

    Everything happens in one writable session and is committed once, only if
    something changed and not under ``dry_run``. Returns the changes made (or,
    with ``dry_run``, that would be made).
    """
    session = repo.writable_session("main")
    changes: list[str] = []

    daily = _open_group(session, config.DAILY_GROUP)
    monthly = _open_group(session, config.MONTHLY_GROUP)

    for group_name, group in (
        (config.DAILY_GROUP, daily),
        (config.MONTHLY_GROUP, monthly),
    ):
        if (
            group is not None
            and "time" in group
            and rechunk_coordinate(group, "time", config.TIME_CHUNKS)
        ):
            changes.append(f"{group_name}/time: rechunked to {config.TIME_CHUNKS}")

    if monthly is not None:
        daily_attrs = dict(daily.attrs) if daily is not None else {}
        changes.extend(fix_monthly_metadata(monthly, daily_attrs))

    if changes and not dry_run:
        session.commit("Repair layout: " + "; ".join(changes))
        logger.info("Repaired layout: %s", "; ".join(changes))
    return changes
