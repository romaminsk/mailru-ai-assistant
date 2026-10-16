"""Локальное обновление полей платежа у уже сохранённых записей. Без облака, без OCR и без почты.

Меняются только новые поля (посредник, назначение, описание, комиссия, отток, идентификаторы).
Подтверждённые, отклонённые и исправленные вручную записи не трогаются. Каждое изменение пишется в историю.
"""

from __future__ import annotations

import json
from typing import Any

from mail_assistant.receipt_extract import verify_receipts
from mail_assistant.receipt_schemas import ReceiptBatch, ReceiptItem
from mail_assistant.storage import Store, utc_now

def refresh_payment_fields(store: Store) -> int:
    changed = 0
    rows = store.conn.execute(
        "SELECT r.*, d.extracted_text, d.text_complete FROM receipts r "
        "JOIN documents d ON d.id = r.document_id "
        "WHERE r.is_current = 1 AND r.manual_edited = 0 AND r.review_status NOT IN ('confirmed', 'rejected') "
        "AND d.extracted_text IS NOT NULL"
    ).fetchall()
    for row in rows:
        item = ReceiptItem(
            merchant_name=row["merchant_name"], merchant_tax_id=row["merchant_tax_id"],
            purchase_date=row["purchase_date"], purchase_time=row["purchase_time"],
            total_amount=row["amount_text"], currency=row["currency"],
            operation_type=row["operation_type"], category=row["category"],
            receipt_number=row["receipt_number"], fiscal_id=row["fiscal_id"],
            document_kind=row["document_kind"], payment_status=row["payment_status"],
            evidence=json.loads(row["evidence_json"]), warnings=[],
        )
        fresh = verify_receipts(ReceiptBatch(receipts=[item]), row["extracted_text"], bool(row["text_complete"]))[0]
        new_values: dict[str, Any] = {
            "intermediary": fresh["intermediary"],
            "purpose": fresh["purpose"],
            "description": fresh["description"],
            "fee_minor": fresh["fee_minor"],
            "debit_minor": fresh["debit_minor"],
            "identifiers_json": json.dumps(fresh["identifiers"], ensure_ascii=False) if fresh["identifiers"] else None,
        }
        updates = {key: value for key, value in new_values.items() if row[key] != value}
        if not updates:
            continue
        now = utc_now()
        with store.conn:
            for key, value in updates.items():
                store.conn.execute(
                    "INSERT INTO receipt_corrections (receipt_id, field, old_value, new_value, changed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row["id"], "local_refresh:" + key, None if row[key] is None else str(row[key]),
                     None if value is None else str(value), now),
                )
            assignments = ", ".join(f"{key} = ?" for key in updates)
            store.conn.execute(
                f"UPDATE receipts SET {assignments}, updated_at = ? WHERE id = ?",
                [*updates.values(), now, row["id"]],
            )
        changed += 1
    return changed
