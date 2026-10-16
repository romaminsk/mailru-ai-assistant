from __future__ import annotations

import pytest

from mail_assistant.presentation import document_label, money_label, preview_summary, review_label
from mail_assistant.receipt_extract import looks_like_receipt, verify_receipts
from mail_assistant.receipt_facts import payment_facts
from mail_assistant.receipt_repair import repair_saved_receipts
from mail_assistant.receipt_schemas import ReceiptBatch, ReceiptItem
from mail_assistant.receipts import ReceiptError, confirm_receipt, edit_receipt
from mail_assistant.storage import Store

TEXT = '''Geely
2 488,00 ВУМ
Чек №123456789
15.10.2026 11:33:49
Получатель платежа: Тестовый Банк, УНП 123456789
Погашение кредита частичное
Сумма: 2 488 ВУМ
Досрочное погашение основного долга: 2488.00;
Код авторизации: 123456
Номер операции в ЕРИП: 12345678'''


def item(**changes):
    fields = dict(merchant_name="Тестовый Банк", purchase_date="2026-10-15", total_amount="2488.00",
                  currency=None, operation_type="expense", category="other",
                  evidence={"merchant": "Тестовый Банк", "date": "15.10.2026 11:33:49",
                            "amount": "Сумма: 2 488 ВУМ", "operation": "Погашение кредита частичное"})
    fields.update(changes)
    return ReceiptItem(**fields)


def verified(text=TEXT, **changes):
    return verify_receipts(ReceiptBatch(receipts=[item(**changes)]), text, True)[0]


def seed(store, text=TEXT, **changes):
    doc = store.create_document("payment", "image/png", 100)
    store.set_document_text(doc, text, True, 1, "text_extracted")
    store.save_result(doc, None, [verified(text, **changes)])
    return store.current_receipts(doc)[0]["id"]


def test_erip_credit_payment_with_integer_amount_and_ocr_currency():
    row = verified()
    assert row["amount_minor"] == 248800
    assert row["currency"] == "BYN"
    assert row["document_kind"] == "payment"
    assert row["payment_status"] == "paid"
    assert row["category"] == "loan"
    assert row["review_status"] != "confirmed"
    assert any("OCR" in warning for warning in row["warnings"])


def test_ocr_currency_alias_requires_erip_context():
    text = TEXT.replace("Номер операции в ЕРИП: 12345678", "")
    assert payment_facts(text)["currency"] is None


def test_conflicting_currencies_are_not_guessed():
    assert payment_facts(TEXT + "\n10.00 USD")["currency"] is None


def test_bank_receipt_does_not_require_itogo_or_fractional_total():
    assert looks_like_receipt("ЕРИП\nСумма: 2 488 BYN")
    assert verified()["amount_text"] == "2488.00"


def test_narrow_space_and_decimal_comma():
    from mail_assistant.money import parse_amount
    assert str(parse_amount("2\u202f488,00")) == "2488.00"


def test_unknown_currency_does_not_hide_amount():
    assert money_label({"amount_text": "2488.00", "amount_minor": None, "currency": None}) == "2\u202f488,00 (валюта не определена)"


def test_negative_net_and_zero_format_without_float():
    assert money_label({"amount_minor": -150, "currency": "BYN"}) == "-1,50 BYN"
    assert money_label({"amount_minor": 0, "currency": "BYN"}) == "0,00 BYN"


def test_quote_must_contain_claimed_amount_not_just_exist():
    row = verified(total_amount="9999.00")
    assert row["amount_text"] is None
    assert row["amount_minor"] is None


def test_optional_belarus_tax_id_is_supported():
    assert item(merchant_tax_id="123456789").merchant_tax_id == "123456789"


def test_no_reference_numbers_or_subtotals_treated_as_total():
    facts = payment_facts(TEXT + "\nИтого начислено 999\nНомер услуги ЕРИП: 4271331")
    assert str(facts["amount"]) == "2488.00"


def test_unpaid_invoice_cannot_be_confirmed_or_included(tmp_path):
    text = TEXT.replace("ВУМ", "BYN") + "\nНе оплачен"
    with Store(tmp_path / "db.sqlite3") as store:
        rid = seed(store, text, currency="BYN", evidence={"merchant": "Тестовый Банк", "date": "15.10.2026 11:33:49",
                                                       "amount": "Сумма: 2 488 BYN", "operation": "Погашение кредита частичное"})
        row = dict(store.receipt(rid))
        assert review_label(row) == "Неоплаченный счёт"
        assert preview_summary([row], set()) == {}
        with pytest.raises(ReceiptError, match="счёт"):
            confirm_receipt(store, rid)
        edit_receipt(store, rid, {"payment_status": "paid"})
        confirm_receipt(store, rid)


def test_saved_currency_repair_is_offline_audited_and_idempotent(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        rid = seed(store)
        with store.conn:
            store.conn.execute("UPDATE receipts SET currency=NULL,amount_minor=NULL,document_kind='unknown',payment_status='unknown',category='other' WHERE id=?", (rid,))
        assert repair_saved_receipts(store) == 1
        row = dict(store.receipt(rid))
        assert row["amount_minor"] == 248800
        assert row["currency"] == "BYN"
        assert row["category"] == "loan"
        assert store.corrections_for(rid)
        assert repair_saved_receipts(store) == 0
        assert row["review_status"] != "confirmed"
        assert preview_summary([row], set())["BYN"]["expense"] == 248800


def test_repair_preserves_manual_fields_and_does_not_call_ai(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        rid = seed(store)
        edit_receipt(store, rid, {"category": "services"})
        assert repair_saved_receipts(store) == 0
        assert store.receipt(rid)["category"] == "services"


def test_uncertain_document_has_clear_human_label():
    label = document_label({"error_code": "LLM_UNCERTAIN", "status": "text_extracted"})
    assert "повтор только вручную" in label
    assert "LLM_UNCERTAIN" not in label


def test_preview_excludes_suspected_duplicates(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        rid = seed(store)
        assert preview_summary([dict(store.receipt(rid))], {rid}) == {}
