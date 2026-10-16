from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from mail_assistant.config import AppError

# Точность ISO 4217 для поддерживаемых валют: все перечисленные используют 2 знака.
SUPPORTED_CURRENCIES = frozenset({"RUB", "USD", "EUR", "KZT", "BYN", "GBP", "CNY", "CHF"})
CENTS = Decimal("0.01")
_AMOUNT_RE = re.compile(r"\d{1,12}(\.\d{1,2})?")
_NUMBER_IN_TEXT_RE = re.compile(r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


class MoneyError(AppError):
    """Некорректная денежная величина; ошибка не превращается в выдуманную сумму."""


def parse_amount(text: str) -> Decimal:
    """Разбор суммы из строки: допускаются пробелы-разделители и запятая или точка перед копейками."""
    if not isinstance(text, str):
        raise MoneyError("Сумма должна быть строкой")
    cleaned = re.sub(r"[\s\u00a0\u202f]", "", text.strip())
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    if not _AMOUNT_RE.fullmatch(cleaned):
        raise MoneyError("Некорректный формат суммы")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise MoneyError("Некорректный формат суммы") from None
    return value.quantize(CENTS)


def amount_to_minor(value: Decimal) -> int:
    """Целое число копеек/центов; Decimal, без float."""
    return int((value * 100).to_integral_value())


def minor_to_text(minor: int) -> str:
    """Строка с двумя знаками: 12345 -> '123.45'. Знак минус допускается для сумм отчёта."""
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 100)
    return f"{sign}{whole}.{frac:02d}"


def normalize_currency(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise MoneyError("Код валюты должен быть из трёх заглавных латинских букв")
    return value


def amounts_in_line(line: str) -> set[Decimal]:
    """Все суммы, встреченные в строке текста (для проверки итога)."""
    found: set[Decimal] = set()
    for match in _NUMBER_IN_TEXT_RE.finditer(line):
        try:
            found.add(parse_amount(match.group(0)))
        except MoneyError:
            continue
    return found
