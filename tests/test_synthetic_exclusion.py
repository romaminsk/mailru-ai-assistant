"""Синтетические документы исключаются из очереди и отчётов автоматически; личные документы не меняются."""

from __future__ import annotations

import sqlite3

from mail_assistant.report import build_report
from mail_assistant.storage import MIGRATIONS, Store
from mail_assistant.web_services import pending_documents


def _legacy_db(path):
    raw = sqlite3.connect(path)
    for version in (1, 2, 3, 4):
        raw.executescript(f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;")
    for sha, part in (("s1", "synthetic_receipt.pdf"), ("s2", "synthetic_scan.png"), ("p1", "2:header.png")):
        raw.execute(
            "INSERT INTO documents (sha256, mime_type, size_bytes, status, created_at, updated_at, extracted_text) "
            "VALUES (?, 'application/pdf', 1, 'text_extracted', 'x', 'x', 'ИТОГО 10,00 RUB')", (sha,))
        doc_id = raw.execute("SELECT id FROM documents WHERE sha256 = ?", (sha,)).fetchone()[0]
        raw.execute("INSERT INTO document_sources (document_id, message_id, part_name, first_seen_at) VALUES (?, NULL, ?, 'x')",
                    (doc_id, part))
    raw.commit()
    raw.close()


def test_migration_v5_flags_only_synthetic_and_keeps_rows(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)

    with Store(path) as store:
        flags = {row["sha256"]: row["is_synthetic"] for row in store.conn.execute("SELECT sha256, is_synthetic FROM documents")}
        assert flags == {"s1": 1, "s2": 1, "p1": 0}
        assert store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_synthetic_document_is_not_in_queue_or_report(tmp_path):
    path = tmp_path / "queue.sqlite3"
    _legacy_db(path)
    with Store(path) as store:
        queue = [row["sha256"] for row in pending_documents(store)]
        assert queue == ["p1"]
        assert build_report(store, "2026-10-01", "2026-10-31")["confirmed_total"] == 0


def test_new_synthetic_import_is_marked_at_creation(tmp_path):
    from receipt_fixtures import FakeOcr, receipt_settings
    from synthetic_docs import make_pdf
    from mail_assistant.receipts import SourceRef, process_document

    settings = receipt_settings(tmp_path)
    with Store(settings.db_path) as store:
        result = process_document(store, make_pdf([{"lines": ["MAGAZIN", "ITOGO 9.00 RUB"]}]),
                                  source=SourceRef(part_name="synthetic_check.pdf"), settings=settings, ocr=FakeOcr())
        assert store.document(result.document_id)["is_synthetic"] == 1
