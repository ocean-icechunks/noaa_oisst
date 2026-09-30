"""CLI option-resolution tests.

These check that ``main_options`` builds the right ``StoreTarget`` from CLI
flags/env vars, without touching a real store: ``store.open_repo`` is
monkeypatched to capture ``ctx.obj`` and short-circuit before any I/O.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from typer.testing import CliRunner

from ohw26_oisst_icechunk import cli, config, daily, rollup, sources, store

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _captured_target(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> store.StoreTarget:
    captured: dict[str, store.StoreTarget] = {}

    def fake_open_repo(target: store.StoreTarget, create: bool = False) -> None:  # noqa: ARG001
        captured["target"] = target
        msg = "stop before touching a real store"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "open_repo", fake_open_repo)
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


def test_none_s3_acl_flag_disables_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _captured_target(monkeypatch, ["--s3-bucket", "b", "--s3-acl", "none"])
    assert target.s3_acl is None


def test_none_s3_acl_flag_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _captured_target(monkeypatch, ["--s3-bucket", "b", "--s3-acl", "None"])
    assert target.s3_acl is None


def test_none_s3_acl_env_var_disables_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OISST_S3_ACL", "none")
    target = _captured_target(monkeypatch, ["--s3-bucket", "b"])
    assert target.s3_acl is None


class _FakeRepo:
    """Stand-in repo: only ``readonly_session`` is ever called in this test."""

    def readonly_session(self, branch: str) -> None:  # noqa: ARG002
        return None


def test_ingest_recent_exits_1_on_a_hole_after_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sources, "anon_s3", lambda: None)
    monkeypatch.setattr(
        sources,
        "dates_available",
        lambda fs, preliminary=False, months=None: (  # noqa: ARG005
            [] if preliminary else [date(2024, 1, 5)]
        ),
    )
    monkeypatch.setattr(
        daily,
        "store_days_state",
        lambda session: {date(2024, 1, 1): False},  # noqa: ARG005
    )
    monkeypatch.setattr(
        store,
        "open_repo",
        lambda target, create=False: _FakeRepo(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["--store-path", "/x", "ingest-recent"])

    assert result.exit_code == 1
    assert "ERROR" in result.output
    assert "missing 2024-01-02..2024-01-04" in result.output


def test_backfill_daily_default_start_matches_config_start_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run_daily_work(
        target: object,  # noqa: ARG001
        months: list[str],
        commit_batch_months: bool,  # noqa: ARG001
        *,
        create: bool = False,  # noqa: ARG001
    ) -> None:
        captured["months"] = months

    monkeypatch.setattr(cli, "_run_daily_work", fake_run_daily_work)

    result = runner.invoke(cli.app, ["--store-path", "/x", "backfill-daily"])

    assert result.exit_code == 0
    assert captured["months"][0] == f"{config.START_DATE:%Y%m}"


def test_backfill_daily_without_create_exits_1_on_missing_store(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli.app,
        ["--store-path", str(tmp_path), "backfill-daily", "--end", "1981-09-01"],
    )

    assert result.exit_code == 1
    assert "ERROR" in result.output
    assert "backfill-daily --create" in result.output


def _fake_populated_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jan 2024 complete+final (ready), Feb 2024 partial with 2 prelim days."""
    state = {date(2024, 1, d): False for d in range(1, 32)}
    state |= {date(2024, 2, 1): False, date(2024, 2, 2): True, date(2024, 2, 3): True}
    monkeypatch.setattr(
        store,
        "open_repo",
        lambda target, create=False: _FakeRepo(),  # noqa: ARG005
    )
    monkeypatch.setattr(daily, "store_days_state", lambda session: state)  # noqa: ARG005
    monkeypatch.setattr(
        store,
        "group_times",
        lambda session, group: np.array(["2023-11-01", "2023-12-01"], "datetime64[ns]"),  # noqa: ARG005
    )


def test_status_text_output_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_populated_store(monkeypatch)

    result = runner.invoke(cli.app, ["--store-path", "/x", "status"])

    assert result.exit_code == 0
    assert result.output == (
        "daily/    34 days  2024-01-01..2024-02-03  "
        "preliminary: 2  calendar-ordered: yes\n"
        "          preliminary days: 2024-02-02..2024-02-03\n"
        "monthly/  2 months  2023-11..2023-12\n"
    )


def test_status_markdown_on_a_populated_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_populated_store(monkeypatch)

    result = runner.invoke(
        cli.app, ["--store-path", "/x", "status", "--format", "markdown"]
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "### Store status"
    assert "| `daily/` | 34 days | 2024-01-01 to 2024-02-03 |" in lines
    assert "| `monthly/` | 2 months | 2023-11 to 2023-12 |" in lines
    assert "- Preliminary days: 2 (2024-02-02 to 2024-02-03)" in lines
    assert "- Calendar-ordered: yes" in lines
    assert "- Ready to roll up, not yet in `monthly/`: 2024-01" in lines


def test_status_markdown_on_an_empty_store(tmp_path: Path) -> None:
    store.open_repo(store.StoreTarget(local_path=tmp_path / "s"), create=True)

    result = runner.invoke(
        cli.app,
        ["--store-path", str(tmp_path / "s"), "status", "--format", "markdown"],
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert "| `daily/` | 0 | (empty) |" in lines
    assert "| `monthly/` | 0 | (empty) |" in lines
    assert "- Preliminary days: 0" in lines
    assert "- Ready to roll up, not yet in `monthly/`: none" in lines


def test_status_rejects_an_unknown_format() -> None:
    result = runner.invoke(
        cli.app, ["--store-path", "/x", "status", "--format", "html"]
    )

    assert result.exit_code == 2


def test_rollup_monthly_continues_after_one_month_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[date] = []

    def fake_rollup_month(
        repo: object,  # noqa: ARG001
        month: date,
        fetch_concurrency: int,  # noqa: ARG001
    ) -> str | None:
        calls.append(month)
        if month == date(2024, 1, 1):
            msg = "boom"
            raise RuntimeError(msg)
        return "snap"

    monkeypatch.setattr(
        store,
        "open_repo",
        lambda target, create=False: _FakeRepo(),  # noqa: ARG005
    )
    monkeypatch.setattr(daily, "store_days_state", lambda session: {})  # noqa: ARG005
    monkeypatch.setattr(
        store,
        "group_times",
        lambda session, group: None,  # noqa: ARG005
    )
    monkeypatch.setattr(
        rollup,
        "months_ready",
        lambda state, monthly_times: [  # noqa: ARG005
            date(2024, 1, 1),
            date(2024, 2, 1),
        ],
    )
    monkeypatch.setattr(rollup, "rollup_month", fake_rollup_month)

    result = runner.invoke(cli.app, ["--store-path", "/x", "rollup-monthly"])

    assert calls == [date(2024, 1, 1), date(2024, 2, 1)]
    assert result.exit_code == 1
    assert "boom" in result.output
