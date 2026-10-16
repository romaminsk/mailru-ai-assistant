from __future__ import annotations

import json

import httpx
import pytest

from receipt_fixtures import (
    RECEIPT_TEXT_LINES,
    FakeLLM,
    FakeOcr,
    ai_options,
    completion,
    llm_settings,
    receipt_json,
    receipt_settings,
)
from synthetic_docs import make_pdf, make_png
from mail_assistant.receipts import (
    ReceiptError,
    SourceRef,
    analyze_document_text,
    confirm_receipt,
    duplicate_suspects,
    edit_receipt,
    process_document,
    reject_receipt,
    review_queue,
)
from mail_assistant.storage import SCHEMA_VERSION, Store

def text_pdf() -> bytes:
    return make_pdf([{"lines": RECEIPT_TEXT_LINES}])


def run(store, data, settings, *, fake=None, ocr=None, budget=5, reprocess=False, source=None, llm=None, allow=True):
    ai = ai_options(fake, budget=budget, llm=llm, allow=allow)
    return process_document(
        store,
        data,
        source=source or SourceRef(part_name="receipt.pdf"),
        settings=settings,
        ocr=ocr or FakeOcr(),
        ai=ai,
        reprocess=reprocess,
    )


def current(store, document_id):
    return [dict(row) for row in store.current_receipts(document_id)]


@pytest.fixture
def settings(tmp_path):
    return receipt_settings(tmp_path)


@pytest.fixture
def store(settings):
    with Store(settings.db_path) as handle:
        yield handle


def test_without_cloud_only_local_text_is_stored_and_no_llm_called(store, settings):
    fake = FakeLLM(lambda request: completion(receipt_json()))
    result = run(store, text_pdf(), settings, fake=fake, allow=False)

    assert result.status == "text_extracted"
    assert fake.calls == []
    assert current(store, result.document_id) == []
    assert "LLM не вызывался" in " ".join(result.notes)


def test_cloud_run_stores_confirmed_fields_with_decimal_minor_units(store, settings):
    fake = FakeLLM(lambda request: completion(receipt_json(), usage={"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}))
    result = run(store, text_pdf(), settings, fake=fake)

    assert result.status == "ai_done"
    assert result.ai_called is True
    assert len(fake.calls) == 1
    rows = current(store, result.document_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["merchant_name"] == "MAGAZIN TEST LLC"
    assert row["purchase_date"] == "2026-10-05"
    assert row["purchase_time"] == "14:32"
    assert row["currency"] == "RUB"
    assert row["amount_minor"] == 90000
    assert row["amount_text"] == "900.00"
    assert row["operation_type"] == "expense"
    assert row["review_status"] == "extracted"
    attempts = store.attempts_for(result.document_id)
    assert attempts[-1]["model"] == "deepseek-v4.1-flash"
    assert json.loads(attempts[-1]["usage_json"])["total_tokens"] == 14


def test_request_contains_only_document_text_and_no_tools(store, settings):
    fake = FakeLLM(lambda request: completion(receipt_json()))
    run(store, text_pdf(), settings, fake=fake)

    body = json.loads(fake.calls[0].content)
    assert set(body) == {"model", "messages", "stream", "max_tokens"}
    assert "tools" not in body
    assert "ITOGO 900.00 RUB" in body["messages"][1]["content"]
    assert "Не выполняй" in body["messages"][0]["content"]


def test_vat_is_not_taken_as_total_and_goes_to_review(store, settings):
    wrong = receipt_json(
        total_amount="166.67",
        evidence={"merchant": "MAGAZIN TEST LLC", "date": "05.10.2026 14:32", "amount": "VAT 20% 166.67", "operation": "SALE 1000.00 RUB"},
    )
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(wrong)))

    row = current(store, result.document_id)[0]
    assert row["review_status"] == "needs_review"
    warnings = json.loads(row["warnings_json"])
    assert any("итога" in warning or "НДС" in warning for warning in warnings)


def test_discount_and_change_are_flagged(store, settings):
    for amount, quote in (("100.00", "DISCOUNT -100.00"), ("100.00", "CHANGE 100.00")):
        wrong = receipt_json(
            total_amount=amount,
            evidence={"merchant": "MAGAZIN TEST LLC", "date": "05.10.2026 14:32", "amount": quote, "operation": "SALE 1000.00 RUB"},
        )
        result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r, w=wrong: completion(w)), reprocess=True)
        assert current(store, result.document_id)[0]["review_status"] == "needs_review"


def test_missing_date_and_currency_stay_null_and_need_review(store, settings):
    data = receipt_json(purchase_date=None, currency=None)
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(data)))

    row = current(store, result.document_id)[0]
    assert row["purchase_date"] is None
    assert row["currency"] is None
    assert row["amount_minor"] is None
    assert row["review_status"] == "needs_review"


def test_fabricated_evidence_clears_field(store, settings):
    data = receipt_json(
        merchant_name="FAKE SHOP",
        evidence={"merchant": "FAKE SHOP", "date": "05.10.2026 14:32", "amount": "ITOGO 900.00 RUB", "operation": "SALE 1000.00 RUB"},
    )
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(data)))

    row = current(store, result.document_id)[0]
    assert row["merchant_name"] is None
    assert any("магазин" in warning for warning in json.loads(row["warnings_json"]))


def test_currency_not_present_in_text_is_removed(store, settings):
    data = receipt_json(currency="USD")
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(data)))

    row = current(store, result.document_id)[0]
    assert row["currency"] is None
    assert row["amount_minor"] is None


def test_refund_operation_needs_explicit_evidence(store, settings):
    refund = receipt_json(
        operation_type="refund",
        evidence={"merchant": "MAGAZIN TEST LLC", "date": "05.10.2026 14:32", "amount": "ITOGO 900.00 RUB", "operation": "REFUND 900"},
    )
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(refund)))

    row = current(store, result.document_id)[0]
    assert row["operation_type"] == "unknown"
    assert row["review_status"] == "needs_review"


def test_multiple_receipts_in_one_document_each_need_review(store, settings):
    two = json.dumps(
        {
            "receipts": [
                json.loads(receipt_json())["receipts"][0],
                json.loads(receipt_json(total_amount="900.00", operation_type="expense"))["receipts"][0],
            ]
        },
        ensure_ascii=False,
    )
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(two)))

    rows = current(store, result.document_id)
    assert [row["ordinal"] for row in rows] == [1, 2]
    assert all(row["review_status"] == "needs_review" for row in rows)


def test_invalid_json_is_not_saved_as_success(store, settings):
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion("это не JSON")))

    assert result.error_code == "LLM_INVALID"
    assert current(store, result.document_id) == []
    assert store.document(result.document_id)["status"] == "text_extracted"


def test_unsupported_currency_is_cleared_and_needs_review(store, settings):
    bad = receipt_json(currency="XYZ")
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(bad)))

    row = current(store, result.document_id)[0]
    assert row["currency"] is None
    assert row["review_status"] == "needs_review"


def test_invalid_tax_id_does_not_reject_whole_response(store, settings):
    bad = receipt_json(merchant_tax_id="INN 77 0123-4567 999")
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(bad)))

    assert result.error_code is None
    assert current(store, result.document_id)[0]["merchant_tax_id"] is None


def test_truncated_model_output_is_not_success(store, settings):
    result = run(
        store,
        text_pdf(),
        settings,
        fake=FakeLLM(lambda r: completion(None, finish="length")),
    )

    assert result.error_code == "LLM_TRUNCATED"
    assert current(store, result.document_id) == []


def test_refusal_is_recorded_without_text(store, settings):
    refusal = {
        "model": "deepseek-v4.1-flash",
        "choices": [{"message": {"role": "assistant", "content": None, "refusal": "SECRET-REFUSAL-TEXT"}, "finish_reason": "stop"}],
    }
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: refusal))

    assert result.error_code == "LLM_REFUSED"
    assert "SECRET-REFUSAL-TEXT" not in json.dumps([dict(row) for row in store.attempts_for(result.document_id)])


def test_uncertain_failure_is_not_retried_automatically(store, settings):
    fake = FakeLLM(lambda r: httpx.Response(500, text="upstream"))
    first = run(store, text_pdf(), settings, fake=fake)
    second = run(store, text_pdf(), settings, fake=fake)

    assert first.error_code == "LLM_UNCERTAIN"
    assert second.error_code == "LLM_UNCERTAIN"
    assert len(fake.calls) == 1
    assert any("reprocess" in note for note in second.notes)


def test_schema_refresh_does_not_retry_uncertain_request(store, settings):
    fake = FakeLLM(lambda r: httpx.Response(500, text="upstream"))
    first = run(store, text_pdf(), settings, fake=fake)
    assert first.error_code == "LLM_UNCERTAIN"

    later = FakeLLM(lambda r: completion(receipt_json()))
    result = analyze_document_text(store, first.document_id, ai_options(later, budget=5), force=True)

    assert later.calls == []
    assert result.error_code == "LLM_UNCERTAIN"
    assert current(store, first.document_id) == []


def test_auth_failure_is_reported_without_key(store, settings):
    fake = FakeLLM(lambda r: httpx.Response(401, text="bad key test-key-123"))
    result = run(store, text_pdf(), settings, fake=fake)

    assert result.error_code == "LLM_FAILED"
    assert len(fake.calls) == 1
    assert "test-key-123" not in json.dumps([dict(row) for row in store.attempts_for(result.document_id)])


def test_budget_exhausted_does_not_call_llm(store, settings):
    fake = FakeLLM(lambda r: completion(receipt_json()))
    result = run(store, text_pdf(), settings, fake=fake, budget=0)

    assert result.error_code == "BUDGET_EXHAUSTED"
    assert fake.calls == []


def test_unconfirmed_provider_blocks_cloud_call(store, settings):
    fake = FakeLLM(lambda r: completion(receipt_json()))
    result = run(store, text_pdf(), settings, fake=fake, llm=llm_settings(email_use_confirmed=False))

    assert result.error_code == "LLM_NOT_CONFIRMED"
    assert fake.calls == []


def test_same_document_in_two_messages_is_processed_once(store, settings):
    fake = FakeLLM(lambda r: completion(receipt_json()))
    data = text_pdf()
    first = run(store, data, settings, fake=fake, source=SourceRef(part_name="a", account="u@mail.ru", mailbox="INBOX", uidvalidity=7, uid=10))
    second = run(store, data, settings, fake=fake, source=SourceRef(part_name="a", account="u@mail.ru", mailbox="INBOX", uidvalidity=7, uid=11))

    assert first.duplicate is False
    assert second.duplicate is True
    assert len(fake.calls) == 1
    assert len(store.sources_for(first.document_id)) == 2
    assert store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_same_uid_in_other_folder_is_separate_message(store, settings):
    data = text_pdf()
    run(store, data, settings, fake=FakeLLM(lambda r: completion(receipt_json())), source=SourceRef(part_name="a", account="u", mailbox="INBOX", uidvalidity=7, uid=10))
    run(store, data, settings, fake=FakeLLM(lambda r: completion(receipt_json())), source=SourceRef(part_name="a", account="u", mailbox="INBOX/ToMyself", uidvalidity=7, uid=10))

    rows = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert rows == 2


def test_rerun_skips_document_already_processed(store, settings):
    fake = FakeLLM(lambda r: completion(receipt_json()))
    run(store, text_pdf(), settings, fake=fake)
    run(store, text_pdf(), settings, fake=fake)

    assert len(fake.calls) == 1


def test_text_is_reused_without_ocr_on_rerun(store, settings):
    ocr = FakeOcr()
    run(store, make_png(), settings, ocr=ocr, allow=False)
    run(store, make_png(), settings, ocr=ocr, allow=False)

    assert ocr.calls == 1


def test_data_survives_restart(settings):
    with Store(settings.db_path) as first:
        result = run(first, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json())))
        confirm_receipt(first, result.receipt_ids[0])
        document_id = result.document_id
    with Store(settings.db_path) as second:
        rows = current(second, document_id)
        assert rows[0]["review_status"] == "confirmed"
        assert rows[0]["amount_minor"] == 90000


def test_confirm_requires_complete_fields(store, settings):
    data = receipt_json(purchase_date=None)
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(data)))
    with pytest.raises(ReceiptError, match="дата"):
        confirm_receipt(store, result.receipt_ids[0])


def test_confirm_flags_partial_text_and_keeps_warning(tmp_path):
    settings = receipt_settings(tmp_path)
    long_lines = RECEIPT_TEXT_LINES + [f"Line {index} " + "x" * 60 for index in range(40)]
    data = make_pdf([{"lines": long_lines}])
    with Store(settings.db_path) as store:
        result = run(store, data, settings, fake=FakeLLM(lambda r: completion(receipt_json())), llm=llm_settings(max_input_chars=500))
        assert result.text_complete is False
        row = current(store, result.document_id)[0]
        assert row["review_status"] == "needs_review"
        assert confirm_receipt(store, row["id"]) is True
        confirmed = store.receipt(row["id"])
        assert confirmed["confirmed_with_partial_text"] == 1


def test_manual_edit_is_recorded_and_recomputes_minor_units(store, settings):
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json())))
    receipt_id = result.receipt_ids[0]
    history = edit_receipt(store, receipt_id, {"amount": "950,50"})

    assert ("amount_text", "900.00", "950.50") in history
    row = store.receipt(receipt_id)
    assert row["amount_minor"] == 95050
    assert row["manual_edited"] == 1
    assert any(c["field"] == "amount_text" and c["new_value"] == "950.50" for c in store.corrections_for(receipt_id))


def test_reprocess_keeps_manual_edit_and_confirmed_totals(store, settings):
    fake = FakeLLM(lambda r: completion(receipt_json()))
    result = run(store, text_pdf(), settings, fake=fake)
    receipt_id = result.receipt_ids[0]
    edit_receipt(store, receipt_id, {"amount": "950.00"})
    confirm_receipt(store, receipt_id)

    other = FakeLLM(lambda r: completion(receipt_json(total_amount="800.00", evidence={"merchant": "MAGAZIN TEST LLC", "date": "05.10.2026 14:32", "amount": "ITOGO 900.00 RUB", "operation": "SALE 1000.00 RUB"})))
    run(store, text_pdf(), settings, fake=other, reprocess=True)

    rows = current(store, result.document_id)
    assert rows[0]["id"] == receipt_id
    assert rows[0]["amount_minor"] == 95000
    versions = store.conn.execute("SELECT COUNT(*) FROM extraction_results WHERE document_id = ?", (result.document_id,)).fetchone()[0]
    assert versions == 2
    non_current = store.conn.execute(
        "SELECT warnings_json FROM receipts WHERE document_id = ? AND is_current = 0", (result.document_id,)
    ).fetchall()
    assert non_current and "не заменила" in non_current[0]["warnings_json"]


def test_reprocess_without_manual_changes_replaces_version(store, settings):
    run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json())))
    document_id = store.conn.execute("SELECT id FROM documents").fetchone()[0]
    run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json(total_amount="900.00"))), reprocess=True)

    assert len(current(store, document_id)) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM receipts WHERE is_current = 1").fetchone()[0] == 1


def test_reject_removes_from_confirmed_and_queue(store, settings):
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json())))
    receipt_id = result.receipt_ids[0]
    confirm_receipt(store, receipt_id)
    reject_receipt(store, receipt_id)

    assert store.receipt(receipt_id)["review_status"] == "rejected"
    assert store.conn.execute("SELECT COUNT(*) FROM confirmed_expenses").fetchone()[0] == 0


def test_review_queue_and_duplicate_suspects_do_not_delete(store, settings):
    run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json())), source=SourceRef(part_name="a"))
    run(store, make_pdf([{"lines": RECEIPT_TEXT_LINES + ["Copy"]}]), settings, fake=FakeLLM(lambda r: completion(receipt_json())), source=SourceRef(part_name="b"))

    assert len(review_queue(store)) == 2
    suspects = duplicate_suspects(store)
    assert suspects and suspects[0]["documents"] == 2
    assert store.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2


def test_edit_rejects_bad_values(store, settings):
    result = run(store, text_pdf(), settings, fake=FakeLLM(lambda r: completion(receipt_json())))
    with pytest.raises(ReceiptError):
        edit_receipt(store, result.receipt_ids[0], {"amount": "1e3"})
    with pytest.raises(ReceiptError):
        edit_receipt(store, result.receipt_ids[0], {"currency": "XXX"})
    with pytest.raises(ReceiptError):
        edit_receipt(store, result.receipt_ids[0], {"unknown": "1"})


def test_migrations_are_versioned_and_not_repeated(settings):
    with Store(settings.db_path) as first:
        version = first.conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in first.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with Store(settings.db_path) as second:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == version == SCHEMA_VERSION
    assert {"accounts", "mailboxes", "messages", "documents", "document_sources", "processing_attempts",
            "extraction_results", "receipts", "receipt_corrections", "confirmed_expenses"} <= tables


def test_database_contains_no_password_columns(store):
    columns = []
    for (table,) in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        columns += [row[1] for row in store.conn.execute(f"PRAGMA table_info({table})").fetchall()]
    assert not any("password" in column.lower() or "secret" in column.lower() for column in columns)


def test_database_file_permissions_are_private(settings):
    import os
    import stat

    with Store(settings.db_path):
        pass
    assert stat.S_IMODE(os.stat(settings.db_path).st_mode) == 0o600


def test_originals_are_stored_only_when_explicitly_enabled(tmp_path):
    import os
    import stat

    off = receipt_settings(tmp_path / "off")
    with Store(off.db_path) as store:
        run(store, text_pdf(), off, allow=False)
    assert not off.originals_dir.exists()

    on = receipt_settings(tmp_path / "on", store_originals=True)
    with Store(on.db_path) as store:
        result = run(store, text_pdf(), on, allow=False, source=SourceRef(part_name="../../evil.pdf"))
    target = on.originals_dir / f"{result.sha256}.bin"
    assert target.exists()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert not any("evil" in name for name in os.listdir(on.originals_dir))


def test_deterministic_failure_is_not_ocred_again(store, settings):
    ocr = FakeOcr(text="")
    first = run(store, make_png(), settings, ocr=ocr, allow=False)
    calls_after_first = ocr.calls
    second = run(store, make_png(), settings, ocr=ocr, allow=False)

    assert first.error_code == "NO_TEXT"
    assert second.error_code == "NO_TEXT"
    assert second.new_document is False
    assert ocr.calls == calls_after_first


def test_environmental_failure_is_retried_after_ocr_becomes_available(store, settings):
    from receipt_fixtures import UnavailableOcr

    data = make_png()
    first = run(store, data, settings, ocr=UnavailableOcr(), allow=False)
    second = run(store, data, settings, ocr=FakeOcr("ИТОГО 500,00 RUB"), allow=False)

    assert first.error_code == "OCR_UNAVAILABLE"
    assert second.error_code is None
    assert second.status == "text_extracted"


def test_new_document_flag_marks_first_sighting_only(store, settings):
    data = text_pdf()
    first = run(store, data, settings, allow=False)
    second = run(store, data, settings, allow=False)

    assert first.new_document is True
    assert first.duplicate is False
    assert second.new_document is False
    assert second.duplicate is True


def test_cloud_gate_blocks_real_calls_until_provider_confirmed(store, settings, monkeypatch):
    import mail_assistant.receipts as receipts_module
    from mail_assistant.receipts import AiOptions, LLMBudget

    monkeypatch.setattr(receipts_module, "OPENCODE_GO_RECEIPTS_CONFIRMED", False)
    monkeypatch.setattr(receipts_module, "LLMClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM вызван")))
    ai = AiOptions(allow_cloud=True, llm_settings=llm_settings(), budget=LLMBudget(5))
    result = process_document(store, text_pdf(), source=SourceRef(part_name="g.pdf"), settings=settings, ocr=FakeOcr(), ai=ai)

    assert result.error_code == "PROVIDER_NOT_CONFIRMED"
    assert result.ai_called is False


def test_non_receipt_text_never_reaches_llm(store, settings):
    from synthetic_docs import make_pdf as pdf

    fake = FakeLLM(lambda r: completion(receipt_json()))
    result = run(store, pdf([{"lines": ["Logo shop", "Welcome back"]}]), settings, fake=fake)

    assert result.error_code == "NOT_RECEIPT"
    assert fake.calls == []


def test_length_is_retried_once_with_4096_tokens(store, settings):
    seen = []

    def responder(request):
        body = json.loads(request.content)
        seen.append(body["max_tokens"])
        if body["max_tokens"] == 800:
            return completion(None, finish="length")
        return completion(receipt_json())

    fake = FakeLLM(responder)
    result = run(store, text_pdf(), settings, fake=fake, budget=5)

    assert seen == [800, 4096]
    assert result.status == "ai_done"
    assert result.ai_called is True


def test_length_retry_respects_request_budget(store, settings):
    fake = FakeLLM(lambda r: completion(None, finish="length"))
    result = run(store, text_pdf(), settings, fake=fake, budget=1)

    assert result.error_code == "LLM_TRUNCATED"
    assert len(fake.calls) == 1


def test_migration_v2_keeps_existing_rows(tmp_path):
    import sqlite3

    from mail_assistant.storage import MIGRATIONS

    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript("BEGIN;\n" + MIGRATIONS[1] + "\nPRAGMA user_version = 1;\nCOMMIT;")
    raw.execute(
        "INSERT INTO documents (sha256, mime_type, size_bytes, status, created_at, updated_at) "
        "VALUES ('abc', 'image/png', 3, 'text_extracted', 'x', 'x')"
    )
    raw.commit()
    raw.close()

    with Store(path) as store:
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE sha256 = 'abc'").fetchone()[0] == 1
        columns = [row[1] for row in store.conn.execute("PRAGMA table_info(processing_attempts)")]
        assert "diagnostics_json" in columns


def test_json_with_surrounding_prose_is_parsed_by_same_schema():
    from mail_assistant.receipt_extract import parse_receipt_batch

    content = 'Вот результат:\n{"receipts": [{"merchant_name": null, "operation_type": "unknown"}]}\nСпасибо {'
    batch = parse_receipt_batch(content)

    assert len(batch.receipts) == 1
    assert batch.receipts[0].operation_type == "unknown"


def test_invalid_json_attempt_stores_safe_diagnostics_only(store, settings):
    fake = FakeLLM(lambda r: completion("не JSON вообще SECRET-REASONING"))
    result = run(store, text_pdf(), settings, fake=fake)

    row = store.attempts_for(result.document_id)[-1]
    assert row["error_code"] == "LLM_INVALID"
    assert "SECRET-REASONING" not in (row["diagnostics_json"] or "")
    assert json.loads(row["diagnostics_json"])["json_error"] == "no_json_object"
