"""Группировка записей в финансовые операции для интерфейса. Чистые функции, без базы и без сети."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from mail_assistant.presentation import CATEGORY_LABELS

NO_PURPOSE = "Назначение ещё не распознано"


def _is_bill(row: dict[str, Any]) -> bool:
    return row.get("payment_status") == "unpaid" or (
        row.get("document_kind") == "invoice" and row.get("payment_status") != "paid"
    )


def _outflow(row: dict[str, Any]) -> int | None:
    value = row.get("debit_minor")
    return value if value is not None else row.get("amount_minor")


def group_operations(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Возвращает (операции, неоплаченные счета). Документы одной операции не удваивают расход."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("review_status") == "rejected":
            continue
        key = f"op:{row['operation_id']}" if row.get("operation_id") else f"rec:{row['id']}"
        groups[key].append(row)
    operations, bills = [], []
    for members in groups.values():
        primary = next((m for m in members if m.get("is_primary")), None) or min(members, key=lambda m: m["id"])
        confirmed = any(m.get("review_status") == "confirmed" for m in members)
        purpose = primary.get("purpose") or primary.get("description")
        item = {
            "date": primary.get("purchase_date"),
            "recipient": primary.get("merchant_name") or "Получатель не указан",
            "purpose": purpose or NO_PURPOSE,
            "purpose_known": bool(purpose),
            "amount_minor": _outflow(primary),
            "amount_text": primary.get("amount_text"),
            "currency": primary.get("currency"),
            "category": CATEGORY_LABELS.get(primary.get("category"), primary.get("category") or "—"),
            "status": "Подтверждён" if confirmed else ("Проверить" if primary.get("review_status") == "needs_review" else "Извлечён"),
            "documents": sorted(members, key=lambda m: m["id"]),
            "document_count": len({m["document_id"] for m in members}),
            "primary_id": primary["id"],
            "intermediary": primary.get("intermediary"),
            "bill": _is_bill(primary),
            "operation_type": primary.get("operation_type"),
            "category_key": primary.get("category"),
            "purchase_date": primary.get("purchase_date"),
        }
        (bills if item["bill"] else operations).append(item)
    operations.sort(key=lambda o: (o["date"] or "", o["primary_id"]))
    bills.sort(key=lambda o: (o["date"] or "", o["primary_id"]))
    return operations, bills


def operation_table(operations: list[dict[str, Any]], money) -> list[dict[str, Any]]:
    """Таблица для интерфейса: Дата | Кому | За что | Сумма | Категория | Проверка."""
    return [
        {
            "Дата": op["date"] or "—",
            "Кому": op["recipient"] if not op["intermediary"] else f"{op['recipient']} (через {op['intermediary']})",
            "За что": op["purpose"],
            "Сумма": money({"amount_minor": op["amount_minor"], "amount_text": op.get("amount_text"), "currency": op["currency"]}),
            "Категория": op["category"],
            "Проверка": op["status"],
        }
        for op in operations
    ]


def operation_summary_rows(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна строка на операцию для сводки: сумма — отток, документы не удваивают расход."""
    return [
        {
            "id": op["primary_id"],
            "amount_minor": op["amount_minor"],
            "currency": op["currency"],
            "operation_type": op["operation_type"],
            "merchant_name": op["recipient"],
            "category": op["category_key"],
            "purchase_date": op["purchase_date"],
            "review_status": "confirmed" if op["status"] == "Подтверждён" else "extracted",
            "document_kind": "payment",
            "payment_status": "paid",
            "amount_text": None,
            "warnings_json": "[]",
        }
        for op in operations
    ]
