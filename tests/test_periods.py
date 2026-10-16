from datetime import date

import pytest

from mail_assistant.periods import describe_period, mail_search_window, subtract_months, three_month_period


def test_today_minus_three_months_example():
    assert describe_period(date(2026, 10, 16)) == "16.07.2026–16.10.2026"


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 5, 31), date(2026, 2, 28)),   # в феврале нет 31 дня
        (date(2026, 11, 30), date(2026, 8, 30)),
        (date(2028, 5, 31), date(2028, 2, 29)),   # високосный год
        (date(2026, 1, 15), date(2025, 10, 15)),  # переход через год
    ],
)
def test_missing_day_uses_last_day_of_month(today, expected):
    assert subtract_months(today, 3) == expected


def test_period_end_is_today_inclusive():
    start, end = three_month_period(date(2026, 10, 16))
    assert end == date(2026, 10, 16)
    assert start == date(2026, 7, 16)


def test_mail_search_has_fourteen_day_margin_before_period():
    start, end = three_month_period(date(2026, 10, 16))
    search_start, search_end = mail_search_window(start, end)
    assert search_start == date(2026, 7, 2)
    assert search_end == end
