from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from mail_assistant.config import AppError
from mail_assistant.money import minor_to_text
from mail_assistant.storage import Store

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ReportError(AppError):
    """Ошибка отчёта; текст безопасен для вывода."""


def parse_period(date_from: str, date_to: str) -> tuple[str, str]:
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        raise ReportError("Даты периода должны быть в формате YYYY-MM-DD") from None
    if start > end:
        raise ReportError("Дата начала периода позже даты окончания")
    return start.isoformat(), end.isoformat()


def _bucket() -> dict[str, int]:
    return {"expense": 0, "refund": 0, "count": 0}


def build_report(store: Store, date_from: str, date_to: str) -> dict[str, Any]:
    """Отчёт строится только из БД по дате покупки. Учитываются только подтверждённые записи."""
    start, end = parse_period(date_from, date_to)
    confirmed = store.conn.execute(
        "SELECT r.id, r.merchant_name, r.purchase_date, r.purchase_time, r.currency, r.amount_minor, "
        "r.operation_type, r.category, r.confirmed_with_partial_text, d.sha256 "
        "FROM receipts r "
        "JOIN confirmed_expenses c ON c.receipt_id = r.id "
        "JOIN documents d ON d.id = r.document_id "
        "WHERE r.is_current = 1 AND r.review_status = 'confirmed' "
        "AND r.payment_status != 'unpaid' AND (r.document_kind != 'invoice' OR r.payment_status = 'paid') "
        "AND r.purchase_date BETWEEN ? AND ? ORDER BY r.purchase_date, r.id",
        (start, end),
    ).fetchall()
    pending_in_period = store.conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE is_current = 1 "
        "AND review_status IN ('extracted', 'needs_review') AND purchase_date BETWEEN ? AND ?",
        (start, end),
    ).fetchone()[0]
    pending_undated = store.conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE is_current = 1 "
        "AND review_status IN ('extracted', 'needs_review') AND purchase_date IS NULL"
    ).fetchone()[0]

    currencies: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"expense": 0, "refund": 0, "count": 0, "merchants": defaultdict(_bucket), "categories": defaultdict(_bucket)}
    )
    partial = 0
    for row in confirmed:
        currency = row["currency"]
        minor = row["amount_minor"]
        if currency is None or minor is None or row["operation_type"] not in ("expense", "refund"):
            continue
        bucket = currencies[currency]
        kind = row["operation_type"]
        bucket[kind] += minor
        bucket["count"] += 1
        merchant = row["merchant_name"] or "—"
        bucket["merchants"][merchant][kind] += minor
        bucket["merchants"][merchant]["count"] += 1
        bucket["categories"][row["category"]][kind] += minor
        bucket["categories"][row["category"]]["count"] += 1
        if row["confirmed_with_partial_text"]:
            partial += 1

    result_currencies = {}
    for currency in sorted(currencies):
        data = currencies[currency]
        result_currencies[currency] = {
            "expense": minor_to_text(data["expense"]),
            "refund": minor_to_text(data["refund"]),
            "net": minor_to_text(data["expense"] - data["refund"]),
            "confirmed_receipts": data["count"],
            "by_merchant": _group_rows(data["merchants"]),
            "by_category": _group_rows(data["categories"]),
        }
    return {
        "period_from": start,
        "period_to": end,
        "basis": "purchase_date",
        "currencies": result_currencies,
        "confirmed_total": len(confirmed),
        "confirmed_with_partial_text": partial,
        "pending_in_period": pending_in_period,
        "pending_without_date": pending_undated,
        "_detail_rows": [dict(row) for row in confirmed],
    }


def _group_rows(groups: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(groups):
        values = groups[key]
        rows.append(
            {
                "name": key,
                "expense": minor_to_text(values["expense"]),
                "refund": minor_to_text(values["refund"]),
                "net": minor_to_text(values["expense"] - values["refund"]),
                "count": values["count"],
            }
        )
    return rows


def render_report(report: dict[str, Any]) -> list[str]:
    lines = [
        f"Отчёт за {report['period_from']} — {report['period_to']} (по дате покупки)",
        f"Подтверждённых чеков: {report['confirmed_total']}",
    ]
    if not report["currencies"]:
        lines.append("Подтверждённых расходов за период нет.")
    for currency, data in report["currencies"].items():
        lines.append("")
        lines.append(f"Валюта {currency}")
        lines.append(f"  Расходы:      {data['expense']}")
        lines.append(f"  Возвраты:     {data['refund']}")
        lines.append(f"  Чистый итог:  {data['net']}")
        lines.append(f"  Чеков:        {data['confirmed_receipts']}")
        lines.append("  По магазинам:")
        for row in data["by_merchant"]:
            lines.append(f"    {row['name']}: расход {row['expense']}, возврат {row['refund']}, итог {row['net']} ({row['count']})")
        lines.append("  По категориям:")
        for row in data["by_category"]:
            lines.append(f"    {row['name']}: расход {row['expense']}, возврат {row['refund']}, итог {row['net']} ({row['count']})")
    lines.append("")
    lines.append(f"Ожидают проверки в периоде: {report['pending_in_period']}")
    lines.append(f"Ожидают проверки без даты покупки: {report['pending_without_date']}")
    lines.append(f"Подтверждено при неполном тексте документа: {report['confirmed_with_partial_text']}")
    return lines


def safe_cell(value: Any) -> str:
    """Защита от formula injection: текст с опасным первым символом получает префикс апострофа."""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


CSV_COLUMNS = (
    "purchase_date", "purchase_time", "merchant", "category", "operation", "currency", "amount",
    "receipt_id", "document_sha256_prefix", "partial_text",
)


def write_csv(report: dict[str, Any], path: Path, *, force: bool = False) -> int:
    if path.exists() and not force:
        raise ReportError("Файл уже существует; укажите --force для перезаписи")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    rows = report["_detail_rows"]
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            minor = row["amount_minor"]
            writer.writerow(
                [
                    safe_cell(row["purchase_date"]),
                    safe_cell(row["purchase_time"]),
                    safe_cell(row["merchant_name"]),
                    safe_cell(row["category"]),
                    safe_cell(row["operation_type"]),
                    safe_cell(row["currency"]),
                    safe_cell(minor_to_text(minor) if minor is not None else ""),
                    safe_cell(row["id"]),
                    safe_cell(row["sha256"][:12]),
                    "yes" if row["confirmed_with_partial_text"] else "no",
                ]
            )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return len(rows)


def report_csv_text(report: dict[str, Any]) -> str:
    """CSV в памяти для скачивания из интерфейса; те же правила защиты, что у файла."""
    import io

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in report["_detail_rows"]:
        minor = row["amount_minor"]
        writer.writerow(
            [
                safe_cell(row["purchase_date"]),
                safe_cell(row["purchase_time"]),
                safe_cell(row["merchant_name"]),
                safe_cell(row["category"]),
                safe_cell(row["operation_type"]),
                safe_cell(row["currency"]),
                safe_cell(minor_to_text(minor) if minor is not None else ""),
                safe_cell(row["id"]),
                safe_cell(row["sha256"][:12]),
                "yes" if row["confirmed_with_partial_text"] else "no",
            ]
        )
    return buffer.getvalue()
