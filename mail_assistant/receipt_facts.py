"""Conservative local facts for receipts, payment confirmations and unpaid bills.

OCR currency corrections are allowed only beside an amount and with ERIP context.
They remain visible warnings, not automatic confirmation of a payment.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from mail_assistant.money import SUPPORTED_CURRENCIES, MoneyError, parse_amount

NUMBER = r"\d{1,12}(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?"
_MONEY = re.compile(
    rf"(?<![\w.,])(?P<amount>{NUMBER})\s*(?P<currency>BYN|RUB|USD|EUR|KZT|GBP|CNY|CHF|ВУМ|ВУN|ВYN|BУN)\b",
    re.I,
)
_LABELED_TOTAL = re.compile(
    rf"^\s*(?:сумма(?:\s+платежа)?|итого(?:\s+к\s+оплате)?|к\s+оплате|всего\s+к\s+оплате|total|itogo)\s*[:：]?\s*(?P<amount>{NUMBER})(?![\d.,])",
    re.I,
)


def payment_facts(text: str) -> dict[str, Any]:
    """Return only unambiguous facts. No amounts inferred from account/receipt IDs."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    erip = bool(re.search(r"ерип|еритг", text, re.I))
    currencies: set[str] = set()
    corrected = False
    for match in _MONEY.finditer(text):
        code = match["currency"].upper()
        if code not in SUPPORTED_CURRENCIES:
            if not erip:
                continue
            code = "BYN"
            corrected = True
        currencies.add(code)
    currency = next(iter(currencies)) if len(currencies) == 1 else None

    # An explicit amount payable / transaction total outranks line items and subtotals.
    candidates: dict[Decimal, str] = {}
    for line in lines:
        match = _LABELED_TOTAL.match(line)
        if match:
            try:
                candidates[parse_amount(match["amount"])] = line
            except MoneyError:
                pass
    amount = next(iter(candidates)) if len(candidates) == 1 else None
    amount_quote = candidates[amount] if amount is not None else None

    unpaid = bool(re.search(r"\bне\s*оплаче[нн]|\bunpaid\b", text, re.I))
    bill = bool(re.search(r"извещени[ея].{0,100}(?:размер|плат)|о размере платы", text, re.I | re.S))
    payment_proof = re.search(r"\bRRN\s*:|код авторизации\s*:|(?:номер|№|N)\s*операции\s*в\s*ЕРИП\s*:|оплачено по услуге\s*:", text, re.I)
    if unpaid:
        kind, status = "invoice", "unpaid"
    elif payment_proof and amount is not None:
        kind, status = "payment", "paid"
    elif bill or re.search(r"сч[её]т\s*(?:на оплату|№)", text, re.I):
        kind, status = "invoice", "unknown"
    else:
        kind, status = "receipt", "unknown"

    category = None
    if re.search(r"погашени[ея]\s+(?:основного\s+долга|кредита)|кредитного договора", text, re.I):
        category = "loan"
    elif re.search(r"жилищно.коммунальн|расчет.жку|расч[её]т.жку", text, re.I):
        category = "utilities"
    elif re.search(r"членский взнос|спортивный клуб", text, re.I):
        category = "entertainment"

    date_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\b", text)
    date_value = time_value = None
    if date_match:
        try:
            parsed = datetime.strptime(date_match[0], "%d.%m.%Y %H:%M:%S")
            date_value, time_value = parsed.date().isoformat(), parsed.time().isoformat()
        except ValueError:
            pass
    number_match = re.search(r"чек\s*(?:№|N[оo]?|N2)\s*(\d{6,})", text, re.I)
    return {
        "currency": currency,
        "currency_corrected": corrected and currency == "BYN",
        "amount": amount,
        "amount_quote": amount_quote,
        "document_kind": kind,
        "payment_status": status,
        "category": category,
        "purchase_date": date_value,
        "purchase_time": time_value,
        "date_quote": date_match[0] if date_value else None,
        "receipt_number": number_match[1] if number_match else None,
        "operation_quote": payment_proof[0] if payment_proof else None,
    }
