from mail_assistant.operation_view import NO_PURPOSE, group_operations


def row(id, **kw):
    base = {"id": id, "document_id": id, "operation_id": None, "is_primary": 0, "purchase_date": "2026-10-15",
            "merchant_name": "Цептер Банк", "purpose": "Погашение кредита частичное", "description": None,
            "debit_minor": 248800, "amount_minor": 248800, "currency": "BYN", "category": "loan",
            "review_status": "extracted", "document_kind": "payment", "payment_status": "paid",
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
