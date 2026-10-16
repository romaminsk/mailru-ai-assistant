"""Скользящий период: сегодня минус три месяца, сегодня включительно. Поиск писем — с запасом."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

SEARCH_MARGIN_DAYS = 14


def subtract_months(value: date, months: int) -> date:
    """Сдвиг на месяцы; если дня нет в целевом месяце — последний день этого месяца."""
    month_index = value.month - 1 - months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(value.day, last_day))


def three_month_period(today: date) -> tuple[date, date]:
    """Период расходов: начало — минус три месяца, конец — сегодня включительно."""
    return subtract_months(today, 3), today


def mail_search_window(period_start: date, period_end: date) -> tuple[date, date]:
    """Окно поиска писем по INTERNALDATE: с запасом до начала периода (письмо может прийти раньше платежа)."""
    return period_start - timedelta(days=SEARCH_MARGIN_DAYS), period_end


def describe_period(today: date) -> str:
    start, end = three_month_period(today)
    return f"{start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"
