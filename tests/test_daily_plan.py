"""Unit tests for the pure daily-ingest planner."""

from __future__ import annotations

from datetime import date

from ohw26_oisst_icechunk.daily import is_calendar_ordered, plan_daily_work


def test_empty_store_appends_everything_in_order() -> None:
    work = plan_daily_work(
        store_days={},
        final_available={date(2024, 1, 2), date(2024, 1, 1)},
        prelim_available={date(2024, 1, 3)},
    )
    assert work.append == [
        (date(2024, 1, 1), False),
        (date(2024, 1, 2), False),
        (date(2024, 1, 3), True),
    ]
    assert work.swap == []
    assert work.skipped_gaps == []


def test_final_wins_over_preliminary_for_new_days() -> None:
    work = plan_daily_work(
        store_days={},
        final_available={date(2024, 1, 1)},
        prelim_available={date(2024, 1, 1)},
    )
    assert work.append == [(date(2024, 1, 1), False)]


def test_present_days_are_skipped() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 1): False},
        final_available={date(2024, 1, 1), date(2024, 1, 2)},
        prelim_available=set(),
    )
    assert work.append == [(date(2024, 1, 2), False)]


def test_preliminary_day_with_final_available_is_swapped() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 1): False, date(2024, 1, 2): True},
        final_available={date(2024, 1, 2)},
        prelim_available=set(),
    )
    assert work.swap == [date(2024, 1, 2)]
    assert work.append == []


def test_preliminary_day_without_final_is_left_alone() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 2): True},
        final_available=set(),
        prelim_available={date(2024, 1, 2)},
    )
    assert not work


def test_gap_older_than_tail_is_skipped_with_warning() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 1): False, date(2024, 1, 3): False},
        final_available={date(2024, 1, 2), date(2024, 1, 4)},
        prelim_available=set(),
    )
    assert work.skipped_gaps == [date(2024, 1, 2)]
    assert work.append == [(date(2024, 1, 4), False)]


def test_hole_after_tail_refuses_append_and_reports_missing() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 1): False},
        final_available={date(2024, 1, 5)},
        prelim_available=set(),
    )
    assert work.append == []
    assert work.missing == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    assert not work


def test_contiguous_append_after_tail_has_no_missing() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 1): False},
        final_available={date(2024, 1, 2)},
        prelim_available=set(),
    )
    assert work.append == [(date(2024, 1, 2), False)]
    assert work.missing == []


def test_empty_store_has_no_contiguity_requirement() -> None:
    work = plan_daily_work(
        store_days={},
        final_available={date(2024, 1, 5)},
        prelim_available=set(),
    )
    assert work.append == [(date(2024, 1, 5), False)]
    assert work.missing == []


def test_no_work_is_falsy() -> None:
    work = plan_daily_work(
        store_days={date(2024, 1, 1): False},
        final_available=set(),
        prelim_available=set(),
    )
    assert not work
    assert not plan_daily_work({}, set(), set())


# --- is_calendar_ordered -------------------------------------------------------


def test_is_calendar_ordered_true_for_strictly_increasing_days() -> None:
    assert is_calendar_ordered([date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)])


def test_is_calendar_ordered_false_for_out_of_order_days() -> None:
    assert not is_calendar_ordered([date(2024, 1, 2), date(2024, 1, 1)])


def test_is_calendar_ordered_false_for_duplicate_days() -> None:
    assert not is_calendar_ordered([date(2024, 1, 1), date(2024, 1, 1)])


def test_is_calendar_ordered_true_for_empty_or_single_day() -> None:
    assert is_calendar_ordered([])
    assert is_calendar_ordered([date(2024, 1, 1)])
