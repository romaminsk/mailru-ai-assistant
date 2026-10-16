from __future__ import annotations

import httpx
import pytest

import mail_assistant.receipt_cli as receipt_cli
import mail_assistant.receipts as receipts_module
from helpers import build_email
from receipt_fixtures import FakeLLM, FakeOcr, UnavailableOcr, completion, receipt_json
from synthetic_docs import make_pdf, make_png
from mail_assistant.__main__ import main
from mail_assistant.llm_client import LLMClient
from mail_assistant.storage import Store
from mail_assistant.sync import candidate_parts

RECEIPT_LINES = [
    "MAGAZIN TEST LLC",
    "INN 7701234567",
    "05.10.2026 14:32",
    "SALE 1000.00 RUB",
    "VAT 20% 166.67",
    "DISCOUNT -100.00",
    "ITOGO 900.00 RUB",
    "CASH 1000.00",
    "CHANGE 100.00",
]


@pytest.fixture
def rcli(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIPTS_DB_PATH", str(tmp_path / "r.sqlite3"))
    monkeypatch.setenv("RECEIPT_ORIGINALS_DIR", str(tmp_path / "originals"))
    monkeypatch.setattr(receipt_cli, "TesseractOcr", lambda *args, **kwargs: FakeOcr())
    return tmp_path


def _pdf_file(tmp_path, name="receipt.pdf", lines=RECEIPT_LINES) -> str:
    path = tmp_path / name
    path.write_bytes(make_pdf([{"lines": lines}]))
    return str(path)


def _patch_llm(monkeypatch, responder):
    fake = FakeLLM(responder)
    monkeypatch.setattr(receipts_module, "LLMClient", lambda llm: LLMClient(llm, transport=httpx.MockTransport(fake.handler)))
    return fake


def _forbid_llm(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("LLM не должен вызываться")

    monkeypatch.setattr(receipts_module, "LLMClient", forbidden)


def test_local_extract_without_cloud_is_text_only_and_hides_text(rcli, monkeypatch, capsys):
    _forbid_llm(monkeypatch)
    path = _pdf_file(rcli)

    assert main(["receipt", "extract", path]) == 0

    out = capsys.readouterr().out
    assert "Статус: text_extracted" in out
    assert "LLM не вызывался" in out
    assert "ITOGO" not in out


def test_show_text_flag_is_explicit_debug_output(rcli, monkeypatch, capsys):
    path = _pdf_file(rcli)

    assert main(["receipt", "extract", path, "--show-text"]) == 0

    assert "ITOGO 900.00 RUB" in capsys.readouterr().out


def test_missing_ocr_is_clear_error_not_fake_result(rcli, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(receipt_cli, "TesseractOcr", lambda *args, **kwargs: UnavailableOcr())
    path = tmp_path / "scan.png"
    path.write_bytes(make_png())

    assert main(["receipt", "extract", str(path)]) == 1

    out = capsys.readouterr().out
    assert "OCR_UNAVAILABLE" in out
    assert "Tesseract" in out
    with Store(rcli / "r.sqlite3") as store:
        assert store.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_cloud_extract_stores_receipt_and_list_show_confirm_report(rcli, llm_env, monkeypatch, capsys):
    fake = _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    path = _pdf_file(rcli)

    assert main(["receipt", "extract", path, "--allow-cloud", "--model", "deepseek-v4.1-flash"]) == 0
    out = capsys.readouterr().out
    assert "Статус: ai_done" in out
    assert "900.00 RUB" in out
    assert len(fake.calls) == 1
    assert "ITOGO" not in out

    assert main(["receipt", "list"]) == 0
    assert "MAGAZIN TEST LLC" in capsys.readouterr().out

    assert main(["receipt", "show", "1"]) == 0
    shown = capsys.readouterr().out
    assert "Цитата (Магазин)" in shown
    assert "ITOGO 900.00 RUB" in shown

    assert main(["receipt", "confirm", "1"]) == 0
    capsys.readouterr()
    assert main(["report", "--from", "2026-10-01", "--to", "2026-10-31"]) == 0
    report = capsys.readouterr().out
    assert "Расходы:      900.00" in report
    assert "Подтверждённых чеков: 1" in report


def test_cloud_blocked_without_confirmed_provider(rcli, llm_env, monkeypatch, capsys):
    monkeypatch.setenv("LLM_EMAIL_USE_CONFIRMED", "false")
    _forbid_llm(monkeypatch)
    path = _pdf_file(rcli)

    assert main(["receipt", "extract", path, "--allow-cloud"]) == 1
    assert "LLM_NOT_CONFIRMED" in capsys.readouterr().out


def test_edit_shows_history_and_reject_removes_from_report(rcli, llm_env, monkeypatch, capsys):
    _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    main(["receipt", "extract", _pdf_file(rcli), "--allow-cloud"])
    capsys.readouterr()

    assert main(["receipt", "edit", "1", "--amount", "910.00", "--category", "services"]) == 0
    out = capsys.readouterr().out
    assert "Исправлено amount_text: 900.00 → 910.00" in out

    assert main(["receipt", "reject", "1"]) == 0
    capsys.readouterr()
    main(["report", "--from", "2026-10-01", "--to", "2026-10-31"])
    assert "Подтверждённых чеков: 0" in capsys.readouterr().out


def test_edit_rejects_unknown_field_and_bad_values(rcli, llm_env, monkeypatch, capsys):
    _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    main(["receipt", "extract", _pdf_file(rcli), "--allow-cloud"])
    capsys.readouterr()

    assert main(["receipt", "edit", "1", "--currency", "XXX"]) == 1
    assert main(["receipt", "edit", "1", "--date", "31.10.2026"]) == 1
    assert main(["receipt", "edit", "1"]) == 1


def test_review_lists_queue(rcli, llm_env, monkeypatch, capsys):
    _patch_llm(monkeypatch, lambda request: completion(receipt_json(purchase_date=None)))
    main(["receipt", "extract", _pdf_file(rcli), "--allow-cloud"])
    capsys.readouterr()

    assert main(["receipt", "review"]) == 0
    assert "На проверку: 1" in capsys.readouterr().out


def test_report_csv_export_from_cli(rcli, llm_env, monkeypatch, capsys, tmp_path):
    _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    main(["receipt", "extract", _pdf_file(rcli), "--allow-cloud"])
    main(["receipt", "confirm", "1"])
    capsys.readouterr()
    target = tmp_path / "export.csv"

    assert main(["report", "--from", "2026-10-01", "--to", "2026-10-31", "--csv", str(target)]) == 0
    assert "строк: 1" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8").splitlines()[0].startswith("purchase_date")


def test_report_runs_without_mail_or_llm_settings(rcli, capsys):
    assert main(["report", "--from", "2026-10-01", "--to", "2026-10-31"]) == 0
    assert "Подтверждённых расходов за период нет." in capsys.readouterr().out


def test_report_rejects_bad_dates(rcli):
    with pytest.raises(SystemExit):
        main(["report", "--from", "2026-13-01", "--to", "2026-10-31"])


def test_sync_requires_explicit_mailbox(rcli):
    with pytest.raises(SystemExit):
        main(["sync", "--received-from", "2026-10-01", "--received-to", "2026-10-31", "--max-messages", "2", "--dry-run"])


def test_sync_real_run_requires_allow_cloud(rcli, mail_env, llm_env, capsys):
    code = main([
        "sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "2",
    ])
    assert code == 1
    assert "--allow-cloud" in capsys.readouterr().err


def test_sync_dry_run_counts_without_bodies_or_llm(rcli, mail_env, fake_server, monkeypatch, capsys):
    _forbid_llm(monkeypatch)
    pdf = make_pdf([{"lines": RECEIPT_LINES}])
    fake_server.add(101, build_email(attachments=(("r.pdf", pdf),)), received=__import__("datetime").date(2026, 10, 2))
    fake_server.add(102, build_email(attachments=(("r.pdf", pdf),)), received=__import__("datetime").date(2026, 10, 3))
    fake_server.add(103, build_email(), received=__import__("datetime").date(2026, 9, 30))
    fake_server.add(104, build_email(attachments=(("r.pdf", pdf),)), received=__import__("datetime").date(2026, 10, 9))

    assert main([
        "sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "2", "--dry-run",
    ]) == 0

    out = capsys.readouterr().out
    assert "Писем в периоде по дате получения: 3" in out
    assert "Писем с PNG/PDF-вложениями (по BODYSTRUCTURE): 3" in out
    assert "Будет обработано (лимит --max-messages 2): 2" in out
    assert "LLM не вызывается" in out
    assert not any("BODY.PEEK[]" in str(call) for call in fake_server.calls)
    assert any(call[0] == "UID" and call[1] == "FETCH" and "BODYSTRUCTURE" in call[3] for call in fake_server.calls)


def test_sync_real_run_is_readonly_peek_and_budgeted(rcli, mail_env, llm_env, fake_server, monkeypatch, capsys):
    fake = _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    fake_server.create("INBOX/Receipts")
    pdf_a = make_pdf([{"lines": RECEIPT_LINES}])
    pdf_b = make_pdf([{"lines": RECEIPT_LINES + ["Second"]}])
    fake_server.add(201, build_email(attachments=(("a.pdf", pdf_a),)), mailbox="INBOX/Receipts")
    fake_server.add(202, build_email(attachments=(("b.pdf", pdf_b),)), mailbox="INBOX/Receipts")

    assert main([
        "sync", "--mailbox", "INBOX/Receipts", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "5", "--allow-cloud", "--max-llm-requests", "1",
    ]) == 0

    out = capsys.readouterr().out
    assert "LLM-запросов в этом запуске: 1" in out
    assert "Отложено из-за лимита LLM-запросов: 1" in out
    assert len(fake.calls) == 1
    selects = [call for call in fake_server.calls if call[0] == "SELECT"]
    assert selects and all(call[2] is True for call in selects)
    assert not any(call[0] in {"STORE", "EXPUNGE", "APPEND", "MOVE"} for call in fake_server.calls)
    fetches = [call for call in fake_server.calls if call[0] == "UID" and call[1] == "FETCH"]
    assert any("BODY.PEEK[]" in call[3] for call in fetches)
    with Store(rcli / "r.sqlite3") as store:
        sources = store.sources_for(1)
        assert sources[0]["mailbox"] == "INBOX/Receipts"
        assert sources[0]["uidvalidity"] == fake_server.uidvalidity


def test_same_attachment_in_two_messages_costs_one_llm_call(rcli, mail_env, llm_env, fake_server, monkeypatch, capsys):
    fake = _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    pdf = make_pdf([{"lines": RECEIPT_LINES}])
    fake_server.add(301, build_email(attachments=(("r.pdf", pdf),)))
    fake_server.add(302, build_email(attachments=(("r.pdf", pdf),)))

    assert main([
        "sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "5", "--allow-cloud", "--max-llm-requests", "5",
    ]) == 0

    assert len(fake.calls) == 1
    with Store(rcli / "r.sqlite3") as store:
        assert store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert len(store.sources_for(1)) == 2


def test_non_receipt_attachments_are_ignored(rcli, mail_env, llm_env, fake_server, monkeypatch, capsys):
    _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    fake_server.add(401, build_email(attachments=(("notes.docx", b"PK\x03\x04 fake docx"),)))

    assert main([
        "sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "5", "--allow-cloud",
    ]) == 0

    with Store(rcli / "r.sqlite3") as store:
        assert store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_candidate_parts_use_content_not_only_name_and_never_write_files(tmp_path):
    pdf = make_pdf([{"lines": RECEIPT_LINES}])
    raw = build_email(attachments=(("../../etc/evil.pdf", pdf), ("fake.png", b"not a png")))

    parts = candidate_parts(raw)

    assert [name for _, name, _ in parts] == ["../../etc/evil.pdf", "fake.png"]
    assert list(tmp_path.iterdir()) == []


def test_mail_check_still_works_without_receipt_settings(fake_server, mail_env, capsys):
    assert main(["mail", "check"]) == 0
    assert "Писем" in capsys.readouterr().out


def test_sync_max_documents_counts_only_new_documents(rcli, mail_env, fake_server, capsys):
    shared = make_pdf([{"lines": RECEIPT_LINES}])
    other = make_pdf([{"lines": RECEIPT_LINES + ["Other"]}])
    fake_server.add(501, build_email(attachments=(("a.pdf", shared),)))
    fake_server.add(502, build_email(attachments=(("a.pdf", shared),)))
    fake_server.add(503, build_email(attachments=(("b.pdf", other),)))

    assert main([
        "sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "5", "--local-only", "--max-documents", "1",
    ]) == 0

    out = capsys.readouterr().out
    assert "Документы, text_extracted: 1" in out
    assert "уже известен (повтор из другого письма): 1" in out
    assert "Документы, отложено (лимит --max-documents): 1" in out
    assert "LLM-запросов в этом запуске: 0" in out


def test_sync_requires_one_explicit_mode(rcli, mail_env, fake_server, capsys):
    base = ["sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31", "--max-messages", "2"]

    assert main(base) == 1
    assert "--allow-cloud или --local-only" in capsys.readouterr().err

    assert main(base + ["--allow-cloud", "--local-only"]) == 1
    assert "только один режим" in capsys.readouterr().err


def test_backup_is_consistent_private_copy(rcli, llm_env, monkeypatch, capsys, tmp_path):
    import os
    import sqlite3
    import stat

    _patch_llm(monkeypatch, lambda request: completion(receipt_json()))
    main(["receipt", "extract", _pdf_file(rcli), "--allow-cloud"])
    main(["receipt", "confirm", "1"])
    capsys.readouterr()
    target = tmp_path / "copy.sqlite3"

    assert main(["receipt", "backup", "--out", str(target)]) == 0

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("SELECT COUNT(*) FROM confirmed_expenses").fetchone()[0] == 1
    finally:
        copy.close()
    assert main(["receipt", "backup", "--out", str(target)]) == 1


def test_sync_kinds_filter_skips_png_when_pdf_requested(rcli, mail_env, fake_server, capsys):
    pdf = make_pdf([{"lines": RECEIPT_LINES}])
    fake_server.add(601, build_email(attachments=(("icon.png", make_png()),)))
    fake_server.add(602, build_email(attachments=(("r.pdf", pdf),)))

    assert main([
        "sync", "--mailbox", "INBOX", "--received-from", "2026-10-01", "--received-to", "2026-10-31",
        "--max-messages", "5", "--dry-run", "--kinds", "pdf",
    ]) == 0
    assert "Писем с PNG/PDF-вложениями (по BODYSTRUCTURE): 1" in capsys.readouterr().out
