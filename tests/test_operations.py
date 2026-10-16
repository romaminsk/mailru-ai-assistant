"""Финансовые операции: один платёж — один расход, связи обратимы, противоречия блокируют объединение."""

from __future__ import annotations

import sqlite3

import pytest

from synthetic_docs import make_pdf
from mail_assistant.operations import (
    apply_plan,
    extract_identifiers,
    plan_links,
    unlink_receipt,
)
from mail_assistant.report import build_report
from mail_assistant.storage import MIGRATIONS, SCHEMA_VERSION, Store

ERIP = [{"type": "erip_operation", "value": "123456789"}]


def _item(ordinal=1, **overrides):
    item = {
        "ordinal": ordinal,
        "merchant_name": "Тестовый Банк",
        "merchant_tax_id": None,
        "purchase_date": "2026-10-15",
        "purchase_time": None,
        "currency": "BYN",
        "amount_minor": 248800,
        "amount_text": "2488.00",
        "operation_type": "expense",
        "category": "loan",
        "receipt_number": None,
        "fiscal_id": None,
        "evidence": {"merchant": None, "date": None, "amount": None, "operation": None},
        "warnings": [],
        "review_status": "extracted",
        "document_kind": "payment",
        "payment_status": "paid",
        "intermediary": "ЕРИП",
        "purpose": "Частичное погашение кредита Geely",
        "description": "Погашение кредита",
        "fee_minor": 0,
        "debit_minor": None,
        "identifiers": ERIP,
    }
    item.update(overrides)
    return item


def _save(store, sha, *items):
    document = store.create_document(sha, "image/png", 10)
    store.save_result(document, None, list(items))
    return document


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "ops.sqlite3") as handle:
        yield handle


def test_identifiers_are_taken_only_from_explicit_labels():
    text = "Номер операции в ЕРИП: 123456789\nRRN: 555666777\nКод авторизации: AB12CD\nНа сумму 10,00 BYN"
    found = {item["type"]: item["value"] for item in extract_identifiers(text)}

    assert found == {"erip_operation": "123456789", "rrn": "555666777", "auth_code": "AB12CD"}


def test_two_documents_of_one_payment_make_one_operation_counted_once(store):
    _save(store, "a" * 64, _item())
    _save(store, "b" * 64, _item())
    plans = plan_links(store)

    assert len(plans) == 1 and plans[0].linked is True
    operation_id = apply_plan(store, plans[0])

    rows = store.conn.execute("SELECT operation_id, is_primary FROM receipts ORDER BY id").fetchall()
    assert {row["operation_id"] for row in rows} == {operation_id}
    assert sum(row["is_primary"] for row in rows) == 1
    for receipt in store.conn.execute("SELECT id FROM receipts").fetchall():
        store.set_review(receipt["id"], "confirmed")
    report = build_report(store, "2026-10-01", "2026-10-31")
    assert report["confirmed_total"] == 1
    assert report["confirmed_documents"] == 2
    assert report["currencies"]["BYN"]["expense"] == "2488.00"


def test_contradicting_amounts_block_automatic_linking(store):
    _save(store, "c" * 64, _item())
    _save(store, "d" * 64, _item(amount_minor=100000, amount_text="1000.00"))
    plans = plan_links(store)

    assert plans and plans[0].linked is False
    assert "разные суммы" in plans[0].reason
    with pytest.raises(Exception):
        apply_plan(store, plans[0])


def test_different_recipients_block_linking(store):
    _save(store, "e" * 64, _item())
    _save(store, "f" * 64, _item(merchant_name="Другой получатель"))

    assert plan_links(store)[0].linked is False


def test_fee_is_not_added_twice_outflow_is_total(store):
    """Услуга 55 + комиссия 2, итог 57: отток 57, не 59."""
    doc = store.create_document("g" * 64, "image/png", 10)
    store.save_result(doc, None, [_item(amount_minor=5700, amount_text="57.00", fee_minor=200, debit_minor=5700,
                                         identifiers=[], purpose="Услуга", description="Услуга")])
    for receipt in store.conn.execute("SELECT id FROM receipts").fetchall():
        store.set_review(receipt["id"], "confirmed")
    report = build_report(store, "2026-10-01", "2026-10-31")

    assert report["currencies"]["BYN"]["expense"] == "57.00"


def test_unlink_is_reversible_and_keeps_history(store):
    _save(store, "h" * 64, _item())
    _save(store, "i" * 64, _item())
    apply_plan(store, plan_links(store)[0])
    target = store.conn.execute("SELECT id FROM receipts ORDER BY id LIMIT 1").fetchone()["id"]

    unlink_receipt(store, target)

    row = store.receipt(target)
    assert row["operation_id"] is None
    link = store.conn.execute("SELECT basis, undone_at FROM operation_links WHERE receipt_id = ?", (target,)).fetchone()
    assert link["undone_at"] is not None
    assert "erip_operation=123456789" in link["basis"]


def test_migration_v4_keeps_existing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    for version in (1, 2, 3):
        raw.executescript(f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;")
    raw.execute(
        "INSERT INTO documents (sha256, mime_type, size_bytes, status, created_at, updated_at) "
        "VALUES ('keep', 'image/png', 1, 'ai_done', 'x', 'x')"
    )
    raw.commit()
    raw.close()

    with Store(path) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE sha256 = 'keep'").fetchone()[0] == 1
        columns = [row[1] for row in store.conn.execute("PRAGMA table_info(receipts)")]
        assert {"operation_id", "purpose", "debit_minor", "is_primary"} <= set(columns)
