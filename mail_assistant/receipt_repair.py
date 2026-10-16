"""Offline, audited repair of old AI results. Never calls IMAP, OCR or a model."""
from __future__ import annotations

import json

from mail_assistant.receipt_extract import verify_receipts
from mail_assistant.receipt_schemas import ReceiptBatch, ReceiptItem
from mail_assistant.storage import Store, utc_now


def repair_saved_receipts(store: Store) -> int:
    changed = 0
    rows = store.conn.execute(
        "SELECT r.*, d.extracted_text, d.text_complete FROM receipts r "
        "JOIN documents d ON d.id = r.document_id WHERE r.is_current = 1 "
        "AND r.manual_edited = 0 AND r.review_status NOT IN ('confirmed', 'rejected')"
    ).fetchall()
    for row in rows:
        if not row["extracted_text"]:
            continue
        item = ReceiptItem(
            merchant_name=row["merchant_name"], merchant_tax_id=row["merchant_tax_id"],
            purchase_date=row["purchase_date"], purchase_time=row["purchase_time"],
            total_amount=row["amount_text"], currency=row["currency"],
            operation_type=row["operation_type"], category=row["category"],
            receipt_number=row["receipt_number"], fiscal_id=row["fiscal_id"],
            document_kind=row["document_kind"], payment_status=row["payment_status"],
            evidence=json.loads(row["evidence_json"]), warnings=[],
        )
        verified = verify_receipts(ReceiptBatch(receipts=[item]), row["extracted_text"], bool(row["text_complete"]))[0]
        # Preserve original model warnings for inspection; don't pretend to have re-run AI.
        warnings = list(dict.fromkeys(json.loads(row["warnings_json"]) + verified["warnings"]))
        updates = {key: verified[key] for key in (
            "currency", "amount_minor", "document_kind", "payment_status", "category"
        ) if row[key] != verified[key]}
        if json.loads(row["warnings_json"]) != warnings:
            updates["warnings_json"] = json.dumps(warnings, ensure_ascii=False)
        if not updates:
            continue
        now = utc_now()
        with store.conn:
            for key, value in updates.items():
                store.conn.execute(
                    "INSERT INTO receipt_corrections (receipt_id, field, old_value, new_value, changed_at) VALUES (?, ?, ?, ?, ?)",
                    (row["id"], "local_repair:" + key, str(row[key]) if row[key] is not None else None,
                     str(value) if value is not None else None, now),
                )
            assignments = ", ".join(f"{key} = ?" for key in updates)
            store.conn.execute(
                f"UPDATE receipts SET {assignments}, updated_at = ? WHERE id = ?",
                [*updates.values(), now, row["id"]],
            )
        changed += 1
    return changed
