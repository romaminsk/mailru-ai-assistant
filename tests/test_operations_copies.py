"""Копии одного документа объединяются только при почти полном совпадении текста и реквизитов."""

from __future__ import annotations

from mail_assistant.operations import apply_plan, plan_text_copies
from mail_assistant.storage import Store

INVOICE = [f"Строка счёта {index}: начислено {index}.00" for index in range(20)]
INVOICE += ["Получатель: ООО Пример", "Итого начислено 33.99 BYN", "PDF сгенерирован 08.10.2026"]


def _doc(store, sha, text_lines, amount=3399):
    document = store.create_document(sha, "application/pdf", 10)
    store.set_document_text(document, "\n".join(text_lines), True, 1, "text_extracted")
    store.save_result(document, None, [{
        "ordinal": 1, "merchant_name": "ООО Пример", "merchant_tax_id": None, "purchase_date": "2026-10-08",
        "purchase_time": None, "currency": "BYN", "amount_minor": amount, "amount_text": f"{amount / 100:.2f}",
        "operation_type": "expense", "category": "services", "receipt_number": None, "fiscal_id": None,
        "evidence": {"merchant": None, "date": None, "amount": None, "operation": None}, "warnings": [],
        "review_status": "needs_review", "document_kind": "invoice", "payment_status": "unpaid",
    }])
    return document


def test_copy_with_only_generation_date_changed_is_linked_as_one_document(tmp_path):
    with Store(tmp_path / "copies.sqlite3") as store:
        _doc(store, "a" * 64, INVOICE)
        _doc(store, "b" * 64, INVOICE[:-1] + ["PDF сгенерирован 15.10.2026"])
        plans = plan_text_copies(store)

        assert len(plans) == 1 and plans[0].linked
        assert "копия документа" in plans[0].reason
        operation_id = apply_plan(store, plans[0])
        ids = {row[0] for row in store.conn.execute("SELECT operation_id FROM receipts WHERE is_current = 1").fetchall()}
        assert ids == {operation_id}


def test_different_amount_is_not_a_copy(tmp_path):
    with Store(tmp_path / "copies2.sqlite3") as store:
        _doc(store, "c" * 64, INVOICE)
        _doc(store, "d" * 64, INVOICE[:-2] + ["Итого начислено 99.00 BYN", "PDF сгенерирован 15.10.2026"], amount=9900)

        assert plan_text_copies(store) == []


def test_different_text_is_not_a_copy_even_with_same_fields(tmp_path):
    with Store(tmp_path / "copies3.sqlite3") as store:
        _doc(store, "e" * 64, INVOICE)
        other = ["Совсем другой документ " + str(index) for index in range(25)]
        _doc(store, "f" * 64, other)

        assert plan_text_copies(store) == []


def test_monthly_invoices_with_different_numbers_are_not_copies(tmp_path):
    """Почти одинаковый текст ежемесячных счетов с разными номерами — разные документы."""
    base = [f"Строка услуги {index}: начислено {index}.00" for index in range(20)]
    with Store(tmp_path / "monthly.sqlite3") as store:
        _doc(store, "1" * 64, base + ["Счет #72323", "Итого 33.99 BYN", "PDF сгенерирован 08.10.2026"])
        _doc(store, "2" * 64, base + ["Счет #72399", "Итого 33.99 BYN", "PDF сгенерирован 15.10.2026"])
        assert plan_text_copies(store) == []


def test_same_invoice_number_with_generation_date_difference_is_copy_with_reason(tmp_path):
    base = [f"Строка услуги {index}: начислено {index}.00" for index in range(20)]
    with Store(tmp_path / "same.sqlite3") as store:
        _doc(store, "3" * 64, base + ["Счет #72323", "Итого 33.99 BYN", "PDF сгенерирован 08.10.2026"])
        _doc(store, "4" * 64, base + ["Счет #72323", "Итого 33.99 BYN", "PDF сгенерирован 15.10.2026"])
        plans = plan_text_copies(store)
        assert len(plans) == 1
        assert "номер счёта ['72323'] совпадает" in plans[0].reason
