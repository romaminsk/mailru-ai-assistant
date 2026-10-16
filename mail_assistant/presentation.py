"""Human-readable UI and cash-flow summaries; no network access."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from mail_assistant.money import parse_amount

CATEGORY_LABELS = {
    "food": "Продукты и питание", "transport": "Транспорт", "health": "Здоровье",
    "household": "Дом", "clothing": "Одежда", "electronics": "Техника",
    "services": "Услуги", "entertainment": "Досуг и спорт", "loan": "Кредиты",
    "utilities": "Коммунальные платежи", "other": "Другое",
}
OPERATION_LABELS = {"expense": "Расход", "refund": "Возврат", "unknown": "Не определено"}
KIND_LABELS = {"receipt": "Чек покупки", "payment": "Квитанция платежа", "invoice": "Счёт к оплате", "unknown": "Документ"}
PAYMENT_LABELS = {"paid": "Оплачен", "unpaid": "Не оплачен", "unknown": "Нет подтверждения оплаты"}


def money_label(row: dict[str, Any]) -> str:
    """An unknown currency must never hide an extracted amount."""
    value = row.get("amount_text")
    parsed = None
    if row.get("amount_minor") is not None:
        parsed = Decimal(row["amount_minor"]) / 100
    elif value is not None:
        parsed = parse_amount(value)
    if parsed is None:
        return "Сумма не распознана"
    amount = f"{parsed:,.2f}".replace(",", "\u202f").replace(".", ",")
    return f"{amount} {row.get('currency') or '(валюта не определена)'}"


def issues(row: dict[str, Any]) -> list[str]:
    result = []
    for key, label in (("amount_text", "Сумма не распознана"), ("currency", "Не определена валюта"),
                       ("purchase_date", "Не определена дата"), ("merchant_name", "Не определён получатель")):
        if row.get(key) is None:
            result.append(label)
    if row.get("operation_type") == "unknown":
        result.append("Не определён тип операции")
    if row.get("payment_status") == "unpaid":
        result.append("Неоплаченный счёт — не расход")
    elif row.get("document_kind") == "invoice" and row.get("payment_status") != "paid":
        result.append("Начисление / счёт: оплата не подтверждена")
    return result


def review_label(row: dict[str, Any], duplicate_ids: set[int] | None = None) -> str:
    if row["review_status"] == "rejected":
        return "Исключён"
    problems = issues(row)
    if row.get("payment_status") == "unpaid":
        return "Неоплаченный счёт"
    if row.get("document_kind") == "invoice" and row.get("payment_status") != "paid":
        return "Счёт: оплата не подтверждена"
    if row["review_status"] == "confirmed":
        return "Подтверждён"
    if duplicate_ids and row["id"] in duplicate_ids:
        return "Возможный дубль"
    return "Нужно исправить: " + "; ".join(problems) if problems else "Распознан · проверьте"


def table_rows(rows: list[dict[str, Any]], duplicate_ids: set[int] | None = None) -> list[dict[str, Any]]:
    return [{
        "№": row["id"], "Дата": row.get("purchase_date") or "Не распознана",
        "Кому / за что": row.get("merchant_name") or "Получатель не распознан",
        "Сумма": money_label(row), "Категория": CATEGORY_LABELS.get(row["category"], row["category"]),
        "Документ": KIND_LABELS.get(row.get("document_kind"), "Документ"),
        "Статус": review_label(row, duplicate_ids),
    } for row in rows]


def preview_summary(rows: list[dict[str, Any]], duplicate_ids: set[int]) -> dict[str, dict[str, Any]]:
    """Separate currencies; exclude invoices, rejected records and every ambiguous duplicate."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["review_status"] == "rejected" or issues(row):
            continue
        if row["id"] in duplicate_ids and row["review_status"] != "confirmed":
            continue
        if row.get("amount_minor") is None:
            continue
        code = row["currency"]
        bucket = grouped.setdefault(code, {"expense": 0, "refund": 0, "count": 0, "pending": 0,
                                            "merchants": defaultdict(int), "categories": defaultdict(int)})
        kind = row["operation_type"]
        if kind not in ("expense", "refund"):
            continue
        amount = row["amount_minor"]
        bucket[kind] += amount
        signed = amount if kind == "expense" else -amount
        bucket["merchants"][row["merchant_name"]] += signed
        bucket["categories"][CATEGORY_LABELS.get(row["category"], row["category"])] += signed
        bucket["count"] += 1
        bucket["pending"] += row["review_status"] != "confirmed"
    return grouped


def document_label(row: dict[str, Any]) -> str:
    code = row.get("error_code")
    if code in ("NOT_RECEIPT", "NO_TEXT"):
        return "Не чек / нет читаемого текста"
    if code == "LLM_UNCERTAIN":
        return "AI: ответ не получен · повтор только вручную"
    if code == "LLM_TRUNCATED":
        return "AI: не хватило лимита ответа"
    if code == "OCR_UNAVAILABLE":
        return "Нужен повтор OCR"
    if row.get("status") == "ai_done":
        return "Обработан"
    if code:
        return "Ошибка обработки · откройте подробности"
    return "Текст готов · ожидает AI" if row.get("chars") else "Ожидает обработки"
