"""CLI option-resolution tests.

These check that ``main_options`` builds the right ``StoreTarget`` from CLI
flags/env vars, without touching a real store: ``store.open_or_create_repo``
is monkeypatched to capture ``ctx.obj`` and short-circuit before any I/O.
"""

from __future__ import annotations

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
