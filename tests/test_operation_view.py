from mail_assistant.operation_view import NO_PURPOSE, group_operations, operation_summary_rows


def row(id, **kw):
    base = {"id": id, "document_id": id, "operation_id": None, "is_primary": 0, "purchase_date": "2026-10-15",
            "merchant_name": "Цептер Банк", "purpose": "Погашение кредита частичное", "description": None,
            "debit_minor": 248800, "amount_minor": 248800, "currency": "BYN", "category": "loan",
            "review_status": "extracted", "document_kind": "payment", "payment_status": "paid", "operation_type": "expense",
            "intermediary": "ЕРИП"}
    base.update(kw)
    return base


def test_documents_of_one_operation_are_one_expense():
    rows = [row(1, operation_id=7, is_primary=1), row(2, operation_id=7)]
    operations, bills = group_operations(rows)

    assert len(operations) == 1 and bills == []
    assert operations[0]["document_count"] == 2
    assert operations[0]["amount_minor"] == 248800


def test_unpaid_invoice_is_separate_and_not_an_operation():
    operations, bills = group_operations([row(3, document_kind="invoice", payment_status="unpaid")])

    assert operations == []
    assert len(bills) == 1


def test_fee_inside_debit_is_not_added_twice():
    operations, _ = group_operations([row(4, debit_minor=5700, amount_minor=5700, purpose="ЧЛЕНСКИЙ ВЗНОС")])

    assert operations[0]["amount_minor"] == 5700


def test_missing_purpose_is_explicit_not_empty():
    operations, _ = group_operations([row(5, purpose=None, description=None)])

    assert operations[0]["purpose"] == NO_PURPOSE
    assert operations[0]["purpose_known"] is False


def test_rejected_rows_are_excluded():
    operations, bills = group_operations([row(6, review_status="rejected")])

    assert operations == [] and bills == []


def test_known_amount_is_visible_even_if_currency_is_unknown():
    from mail_assistant.operation_view import operation_table
    from mail_assistant.presentation import money_label

    operations, _ = group_operations([row(6, debit_minor=None, amount_minor=None,
                                          amount_text="2488.00", currency=None)])
    assert operation_table(operations, money_label)[0]["Сумма"] == "2\u202f488,00 (валюта не определена)"


def test_recipient_summary_groups_typographical_variants_but_not_different_legal_forms():
    from mail_assistant.presentation import preview_summary

    entries = [row(7, merchant_name='ЗАО "Тест Банк"'),
               row(8, merchant_name='ЗАО «Тест Банк»'),
               row(9, merchant_name='Закрытое акционерное общество "Тест Банк"'),
               row(10, merchant_name='ОАО "Тест Банк"')]
    operations, _ = group_operations(entries)
    totals = preview_summary(operation_summary_rows(operations), set())['BYN']
    assert len(operations) == 4  # display grouping must never delete payments
    assert len(totals['merchants']) == 2
    assert totals['merchants']['ЗАО «Тест Банк»'] == 3 * 248800
    assert totals['merchants']['ОАО «Тест Банк»'] == 248800
