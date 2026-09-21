"""CLI option-resolution tests.

These check that ``main_options`` builds the right ``StoreTarget`` from CLI
flags/env vars, without touching a real store: ``store.open_or_create_repo``
is monkeypatched to capture ``ctx.obj`` and short-circuit before any I/O.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from ohw26_oisst_icechunk import cli, store

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


def _captured_target(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> store.StoreTarget:
    captured: dict[str, store.StoreTarget] = {}

    def fake_open_or_create_repo(target: store.StoreTarget) -> None:
        captured["target"] = target
        msg = "stop before touching a real store"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "open_or_create_repo", fake_open_or_create_repo)
    monkeypatch.setattr(cli.store, "open_or_create_repo", fake_open_or_create_repo)
    runner.invoke(cli.app, [*args, "status"])
    return captured["target"]


def test_default_s3_acl_is_bucket_owner_full_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _captured_target(monkeypatch, ["--s3-bucket", "b"])
    assert target.s3_acl == "bucket-owner-full-control"


def test_empty_s3_acl_flag_disables_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _captured_target(monkeypatch, ["--s3-bucket", "b", "--s3-acl", ""])
    assert target.s3_acl is None


class _FakeRepo:
    """Stand-in repo: only ``readonly_session`` is ever called in this test."""

    def readonly_session(self, branch: str) -> None:  # noqa: ARG002
        return None


def test_ingest_recent_exits_1_on_a_hole_after_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.sources, "anon_s3", lambda: None)
    monkeypatch.setattr(
        cli.sources,
        "dates_available",
        lambda fs, preliminary=False, months=None: (  # noqa: ARG005
            [] if preliminary else [date(2024, 1, 5)]
        ),
    )
    monkeypatch.setattr(
        cli.daily,
        "store_days_state",
        lambda session: {date(2024, 1, 1): False},  # noqa: ARG005
    )
    monkeypatch.setattr(
        cli.store,
        "open_or_create_repo",
        lambda target: _FakeRepo(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["--store-path", "/x", "ingest-recent"])

    assert result.exit_code == 1
    assert "ERROR" in result.output
    assert "missing 2024-01-02..2024-01-04" in result.output
