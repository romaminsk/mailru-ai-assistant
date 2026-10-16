"""Поля платежа на синтетических аналогах: назначение, посредник, комиссия и отток."""

from __future__ import annotations

from decimal import Decimal

from mail_assistant.payment_fields import payment_fields
from mail_assistant.receipt_extract import verify_receipts
from mail_assistant.receipt_schemas import ReceiptBatch, ReceiptItem

MEMBERSHIP = """Получатель платежа: "СПОРТИВНЫЙ КЛУБ ПРИМЕР"
Назначение платежа: ЧЛЕНСКИЙ ВЗНОС
Оплачено по услуге: 55.00 BYN
Комиссия: 2.00 BYN
Сумма: 57.00 BYN
Номер операции в ЕРИП: 123456789
Платёж через ЕРИП"""

UTILITIES = """Оплата через ЕРИП
Получатель платежа: ОАО "НКФО "ЕРИП"
Сумма: 173,07 BYN
Номер операции в ЕРИП: 6926595017"""

PLAIN = """Магазин Пример
05.10.2026 14:32
Итого 12.50 BYN"""


def _item(**overrides):
    fields = dict(
        merchant_name="СПОРТИВНЫЙ КЛУБ ПРИМЕР", purchase_date=None, total_amount="57.00", currency="BYN",
        operation_type="expense", category="entertainment",
        evidence={"merchant": "СПОРТИВНЫЙ КЛУБ ПРИМЕР", "date": None, "amount": "Сумма: 57.00 BYN",
                  "operation": "ЧЛЕНСКИЙ ВЗНОС"},
    )
    fields.update(overrides)
    return ReceiptItem(**fields)


def test_membership_fee_outflow_is_total_not_sum_with_fee():
    fields = payment_fields(MEMBERSHIP)

    assert fields["purpose"] == "ЧЛЕНСКИЙ ВЗНОС"
    assert fields["purpose_quote"] == "Назначение платежа: ЧЛЕНСКИЙ ВЗНОС"
    assert fields["fee_amount"] == Decimal("2.00")
    assert fields["service_amount"] == Decimal("55.00")
    assert fields["debit_amount"] == Decimal("57.00")
    assert fields["notes"] == []


def test_membership_verified_row_stores_debit_57_and_fee_2():
    row = verify_receipts(ReceiptBatch(receipts=[_item()]), MEMBERSHIP, True)[0]

    assert row["amount_minor"] == 5700
    assert row["debit_minor"] == 5700
    assert row["fee_minor"] == 200
    assert row["purpose"] == "ЧЛЕНСКИЙ ВЗНОС"
    assert row["description"] == "ЧЛЕНСКИЙ ВЗНОС"
    assert {"type": "erip_operation", "value": "123456789"} in row["identifiers"]


def test_erip_is_intermediary_and_recipient_is_not_invented():
    fields = payment_fields(UTILITIES)

    assert fields["intermediary"] == "ЕРИП"
    assert fields["purpose"] is None
    assert fields["fee_amount"] is None


def test_erip_platform_as_recipient_gets_warning_not_fake_payee():
    item = _item(merchant_name='ОАО "НКФО "ЕРИП"', total_amount="173.07",
                 evidence={"merchant": 'ОАО "НКФО "ЕРИП"', "date": None, "amount": "Сумма: 173,07 BYN",
                           "operation": None})
    row = verify_receipts(ReceiptBatch(receipts=[item]), UTILITIES, True)[0]

    assert row["intermediary"] == "ЕРИП"
    assert any("платёжная система ЕРИП" in warning for warning in row["warnings"])


def test_plain_receipt_has_no_invented_purpose_or_fee():
    row = verify_receipts(ReceiptBatch(receipts=[_item(total_amount="12.50", evidence={
        "merchant": "СПОРТИВНЫЙ КЛУБ ПРИМЕР", "date": None, "amount": "Итого 12.50 BYN", "operation": None})]),
        PLAIN, True)[0]

    assert row["purpose"] is None
    assert row["fee_minor"] is None
    assert row["debit_minor"] == 1250 or row["debit_minor"] is None


def test_mismatch_between_service_fee_and_total_is_flagged_and_total_is_used():
    text = MEMBERSHIP.replace("Сумма: 57.00 BYN", "Сумма: 59.00 BYN")
    fields = payment_fields(text)

    assert fields["debit_amount"] == Decimal("59.00")
    assert "не сходятся" in " ".join(fields["notes"])


def test_missing_total_uses_service_plus_fee_with_note():
    text = "Оплачено по услуге: 55.00 BYN\nКомиссия: 2.00 BYN"
    fields = payment_fields(text)

    assert fields["debit_amount"] == Decimal("57.00")
    assert any("вычислен" in note for note in fields["notes"])


def test_refresh_updates_only_payment_fields_and_keeps_confirmed(tmp_path):
    from mail_assistant.payment_refresh import refresh_payment_fields
    from mail_assistant.storage import Store
    from synthetic_docs import make_pdf

    with Store(tmp_path / "refresh.sqlite3") as store:
        doc = store.create_document("r" * 64, "application/pdf", 10)
        store.set_document_text(doc, MEMBERSHIP, True, 1, "text_extracted")
        store.save_result(doc, None, [{
            "ordinal": 1, "merchant_name": "СПОРТИВНЫЙ КЛУБ ПРИМЕР", "merchant_tax_id": None, "purchase_date": None,
            "purchase_time": None, "currency": "BYN", "amount_minor": 5700, "amount_text": "57.00",
            "operation_type": "expense", "category": "entertainment", "receipt_number": None, "fiscal_id": None,
            "evidence": {"merchant": "СПОРТИВНЫЙ КЛУБ ПРИМЕР", "date": None, "amount": "Сумма: 57.00 BYN",
                         "operation": "ЧЛЕНСКИЙ ВЗНОС"},
            "warnings": [], "review_status": "needs_review", "document_kind": "payment", "payment_status": "paid",
        }])
        receipt_id = store.current_receipts(doc)[0]["id"]
        assert refresh_payment_fields(store) == 1
        row = store.receipt(receipt_id)
        assert row["purpose"] == "ЧЛЕНСКИЙ ВЗНОС"
        assert row["debit_minor"] == 5700 and row["fee_minor"] == 200
        assert row["amount_minor"] == 5700
        assert refresh_payment_fields(store) == 0
        store.set_review(receipt_id, "confirmed")
        assert refresh_payment_fields(store) == 0
