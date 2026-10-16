"""Поля платежа из явных подписей документа: назначение, получатель, посредник, комиссия, отток.

Каждое значение берётся из строки с подписью и сохраняет цитату этой строки. Модель не используется,
значения не выводятся из соседних чисел. Отсутствующее поле остаётся пустым.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mail_assistant.money import MoneyError, parse_amount

_MONEY = r"(?P<amount>\d{1,12}(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?)"
_LABELS = {
    "purpose": re.compile(r"назначение\s+платежа\s*:\s*(?P<value>.+)", re.I),
    "recipient": re.compile(r"получатель\s+платежа\s*:\s*(?P<value>.+)", re.I),
    "service": re.compile(rf"оплачено\s+по\s+услуге\s*:\s*{_MONEY}", re.I),
    "fee": re.compile(rf"комиссия\s*:\s*{_MONEY}", re.I),
    "total": re.compile(rf"^\s*сумма\s*:\s*{_MONEY}", re.I | re.M),
}
_ERIP_RE = re.compile(r"\bЕРИП\b|\bERIP\b", re.I)
# Строки операции, которые описывают «за что» платёж, без подписи «Назначение платежа».
_PURPOSE_HINT = re.compile(
    r"погашени|оплат|платеж|взнос|услуг|перевод|возврат|покупк|кредит|аренд|штраф|пополнени|абонент",
    re.I,
)
# Строки реквизитов: это не назначение, даже если содержат слова из подсказок.
_REQUISITE = re.compile(
    r"договор|номер|№|\bсч[её]т|\bИНН\b|\bУНП\b|получател|плательщик|адрес|\bкод\b|\bБИК\b|\bIBAN\b|RRN|"
    r"чек|ЕРИП|операци[ияи]\s+в\s+ЕРИП|авторизац",
    re.I,
)



@dataclass(frozen=True)
class Labeled:
    value: Any
    quote: str


def _first_line(text: str, label: str) -> Labeled | None:
    pattern = _LABELS[label]
    for line in text.splitlines():
        match = pattern.search(line.strip())
        if match:
            value = match.groupdict().get("value")
            if value is not None:
                value = value.strip().strip('"«»').strip()
                return Labeled(value, line.strip()) if value else None
            return Labeled(match.group("amount"), line.strip())
    return None


def _amount(labeled: Labeled | None) -> Decimal | None:
    if labeled is None:
        return None
    try:
        return parse_amount(labeled.value)
    except MoneyError:
        return None


def _purpose_from_operation_lines(text: str) -> Labeled | None:
    """Назначение без подписи: первая строка операции, где есть смысл платежа, а не реквизит.

    Ищется по всему документу, а не по одной подписи: назначение может стоять отдельной строкой
    («Погашение кредита частичное») или в строке суммы («Досрочное погашение основного долга: 2488.00»).
    """
    for line in text.splitlines():
        clean = line.strip()
        if not clean or _REQUISITE.search(clean) or not _PURPOSE_HINT.search(clean):
            continue
        value = re.sub(r":?\s*\d{1,12}(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?;?\s*(?:BYN|RUB|USD|EUR|ВУМ)?\s*$", "", clean)
        value = value.strip(" :;.")
        if len(value) >= 4:
            return Labeled(value, clean)
    return None


def payment_fields(text: str) -> dict[str, Any]:
    """Поля платежа с цитатами. Пустые значения означают «не указано в документе»."""
    purpose = _first_line(text, "purpose") or _purpose_from_operation_lines(text)
    recipient = _first_line(text, "recipient")
    service = _amount(_first_line(text, "service"))
    fee_line = _first_line(text, "fee")
    fee = _amount(fee_line)
    total_line = _first_line(text, "total")
    total = _amount(total_line)

    intermediary = None
    intermediary_quote = None
    if recipient and _ERIP_RE.search(recipient.value):
        intermediary, intermediary_quote = "ЕРИП", recipient.quote
    elif _ERIP_RE.search(text) and re.search(r"платеж\w*\s+ЕРИП|через\s+ЕРИП|ЕРИП\s*:", text, re.I):
        intermediary, intermediary_quote = "ЕРИП", "ЕРИП"

    notes: list[str] = []
    debit = total
    if service is not None and fee is not None and total is not None and service + fee != total:
        notes.append("Оплачено по услуге и комиссия не сходятся с итогом; отток взят по итогу")
    if fee is not None and total is None and service is not None:
        debit = service + fee
        notes.append("Итог не указан: отток вычислен как услуга + комиссия")

    return {
        "purpose": purpose.value if purpose else None,
        "purpose_quote": purpose.quote if purpose else None,
        "recipient": recipient.value if recipient else None,
        "recipient_quote": recipient.quote if recipient else None,
        "intermediary": intermediary,
        "intermediary_quote": intermediary_quote,
        "service_amount": service,
        "fee_amount": fee,
        "debit_amount": debit,
        "total_quote": total_line.quote if total_line else None,
        "notes": notes,
    }
