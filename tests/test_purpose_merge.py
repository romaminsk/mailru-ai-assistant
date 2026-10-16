"""Назначение: ответ модели с цитатой побеждает; локальные правила не заменяют и не обедняют результат."""

from __future__ import annotations

from mail_assistant.receipt_extract import verify_receipts
from mail_assistant.receipt_schemas import ReceiptBatch, ReceiptItem

CREDIT = """Получатель платежа: Закрытое акционерное общество Пример
Погашение кредита частичное
Номер кредитного договора А/500086
Досрочное погашение основного долга: 2488.00;
Сумма: 2 488 BYN
Номер операции в ЕРИП: 6926610184"""


def _item(**overrides):
    fields = dict(
        merchant_name="Цептер Банк", purchase_date="2026-10-15", total_amount="2488.00", currency="BYN",
        operation_type="expense", category="loan", document_kind="payment", payment_status="paid",
        evidence={"merchant": "Получатель платежа", "date": None, "amount": "Сумма: 2 488 BYN",
                  "operation": "Погашение кредита частичное", "purpose": "Погашение кредита частичное", "fee": None},
    )
    fields.update(overrides)
    return ReceiptItem(**fields)


def test_model_purpose_with_quote_is_kept():
    row = verify_receipts(ReceiptBatch(receipts=[_item(
        purpose="Частичное погашение кредита", description="Частичное погашение кредита")]), CREDIT, True)[0]

    assert row["purpose"] == "Частичное погашение кредита"
    assert row["description"] == "Частичное погашение кредита"


def test_local_rule_fills_only_empty_purpose():
    row = verify_receipts(ReceiptBatch(receipts=[_item(purpose=None)]), CREDIT, True)[0]

    assert row["purpose"] == "Погашение кредита частичное"


def test_local_rule_never_replaces_model_purpose():
    row = verify_receipts(ReceiptBatch(receipts=[_item(purpose="Погашение кредита Geely")]), CREDIT, True)[0]

    assert row["purpose"] == "Погашение кредита Geely" or any("без подтверждающей цитаты" in w for w in row["warnings"])
    assert row["purpose"] != "Досрочное погашение основного долга"


def test_model_purpose_without_quote_is_dropped_with_warning():
    item = _item(purpose="Выдуманное назначение", evidence={"merchant": None, "date": None, "amount": None,
                                                              "operation": None, "purpose": "нет такой строки", "fee": None})
    row = verify_receipts(ReceiptBatch(receipts=[item]), CREDIT, True)[0]

    assert row["purpose"] == "Погашение кредита частичное"
    assert any("без подтверждающей цитаты" in w for w in row["warnings"])


def test_missing_label_is_not_missing_purpose():
    text = "Получатель платежа: Пример\nПогашение кредита частичное\nСумма: 2 488 BYN"
    row = verify_receipts(ReceiptBatch(receipts=[_item(purpose=None, evidence={
        "merchant": None, "date": None, "amount": "Сумма: 2 488 BYN", "operation": None, "purpose": None, "fee": None})]),
        text, True)[0]

    assert row["purpose"] == "Погашение кредита частичное"


def test_refresh_does_not_overwrite_model_purpose(tmp_path):
    from mail_assistant.payment_refresh import refresh_payment_fields
    from mail_assistant.storage import Store

    with Store(tmp_path / "merge.sqlite3") as store:
        doc = store.create_document("m" * 64, "image/png", 10)
        store.set_document_text(doc, CREDIT, True, 1, "text_extracted")
        store.save_result(doc, None, [{
            "ordinal": 1, "merchant_name": "Цептер Банк", "merchant_tax_id": None, "purchase_date": "2026-10-15",
            "purchase_time": None, "currency": "BYN", "amount_minor": 248800, "amount_text": "2488.00",
            "operation_type": "expense", "category": "loan", "receipt_number": None, "fiscal_id": None,
            "evidence": {"merchant": None, "date": None, "amount": None, "operation": None},
            "warnings": [], "review_status": "needs_review", "document_kind": "payment", "payment_status": "paid",
            "purpose": "Частичное погашение кредита", "description": "Частичное погашение кредита",
        }])
        rid = store.current_receipts(doc)[0]["id"]
        refresh_payment_fields(store)

        assert store.receipt(rid)["purpose"] == "Частичное погашение кредита"
