from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mail_assistant.config import AppError

SCHEMA_VERSION = 5
REVIEW_STATUSES = ("extracted", "needs_review", "confirmed", "rejected")
OPERATION_TYPES = ("expense", "refund", "unknown")
EDITABLE_COLUMNS = frozenset(
    {"merchant_name", "purchase_date", "purchase_time", "currency", "amount_minor", "amount_text", "category",
     "operation_type", "document_kind", "payment_status", "debit_minor"}
)
CATEGORIES = ("food", "transport", "health", "household", "clothing", "electronics", "services", "entertainment", "loan", "utilities", "other")

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY,
        identifier TEXT NOT NULL UNIQUE
    );
    CREATE TABLE mailboxes (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        name TEXT NOT NULL,
        uidvalidity INTEGER NOT NULL,
        UNIQUE (account_id, name, uidvalidity)
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id),
        uid INTEGER NOT NULL,
        received_date TEXT,
        first_seen_at TEXT NOT NULL,
        UNIQUE (mailbox_id, uid)
    );
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        status TEXT NOT NULL,
        error_code TEXT,
        page_count INTEGER,
        text_complete INTEGER NOT NULL DEFAULT 0,
        extracted_text TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE document_sources (
        id INTEGER PRIMARY KEY,
        document_id INTEGER NOT NULL REFERENCES documents(id),
        message_id INTEGER REFERENCES messages(id),
        part_name TEXT NOT NULL,
        first_seen_at TEXT NOT NULL
    );
    CREATE TABLE processing_attempts (
        id INTEGER PRIMARY KEY,
        document_id INTEGER NOT NULL REFERENCES documents(id),
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        error_code TEXT,
        model TEXT,
        usage_json TEXT,
        extractor_version TEXT NOT NULL,
        prompt_version TEXT,
        schema_version TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL
    );
    CREATE TABLE extraction_results (
        id INTEGER PRIMARY KEY,
        document_id INTEGER NOT NULL REFERENCES documents(id),
        attempt_id INTEGER REFERENCES processing_attempts(id),
        version INTEGER NOT NULL,
        is_current INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (document_id, version)
    );
    CREATE TABLE receipts (
        id INTEGER PRIMARY KEY,
        document_id INTEGER NOT NULL REFERENCES documents(id),
        result_id INTEGER NOT NULL REFERENCES extraction_results(id),
        ordinal INTEGER NOT NULL,
        is_current INTEGER NOT NULL,
        merchant_name TEXT,
        merchant_tax_id TEXT,
        purchase_date TEXT,
        purchase_time TEXT,
        currency TEXT,
        amount_minor INTEGER,
        amount_text TEXT,
        operation_type TEXT NOT NULL,
        category TEXT NOT NULL,
        receipt_number TEXT,
        fiscal_id TEXT,
        evidence_json TEXT NOT NULL,
        warnings_json TEXT NOT NULL,
        review_status TEXT NOT NULL,
        manual_edited INTEGER NOT NULL DEFAULT 0,
        confirmed_with_partial_text INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE receipt_corrections (
        id INTEGER PRIMARY KEY,
        receipt_id INTEGER NOT NULL REFERENCES receipts(id),
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        changed_at TEXT NOT NULL
    );
    CREATE TABLE confirmed_expenses (
        receipt_id INTEGER PRIMARY KEY REFERENCES receipts(id),
        confirmed_at TEXT NOT NULL
    );
    CREATE INDEX receipts_current ON receipts(document_id, ordinal, is_current);
    CREATE INDEX receipts_purchase ON receipts(purchase_date, currency);
    """,
    # Безопасная диагностика попытки: только allowlist-метаданные, без текстов ответа и письма.
    2: """
    ALTER TABLE processing_attempts ADD COLUMN diagnostics_json TEXT;
    """,
    3: """
    ALTER TABLE receipts ADD COLUMN document_kind TEXT NOT NULL DEFAULT 'unknown';
    ALTER TABLE receipts ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'unknown';
    """,    # Финансовые операции: один платёж или покупка может иметь несколько документов.
    # Расход учитывается по операции, а не по документу. Связи сохраняют основание и обратимы.
    # Синтетические тестовые документы: только по имени источника synthetic_*. Личные документы не меняются.
    5: """
    ALTER TABLE documents ADD COLUMN is_synthetic INTEGER NOT NULL DEFAULT 0;
    UPDATE documents SET is_synthetic = 1
     WHERE id IN (SELECT document_id FROM document_sources WHERE part_name LIKE 'synthetic\\_%' ESCAPE '\\')
       AND id NOT IN (SELECT document_id FROM document_sources WHERE part_name NOT LIKE 'synthetic\\_%' ESCAPE '\\');
    """,
    4: """
    ALTER TABLE receipts ADD COLUMN intermediary TEXT;
    ALTER TABLE receipts ADD COLUMN purpose TEXT;
    ALTER TABLE receipts ADD COLUMN description TEXT;
    ALTER TABLE receipts ADD COLUMN fee_minor INTEGER;
    ALTER TABLE receipts ADD COLUMN debit_minor INTEGER;
    ALTER TABLE receipts ADD COLUMN identifiers_json TEXT;
    ALTER TABLE receipts ADD COLUMN operation_id INTEGER REFERENCES operations(id);
    ALTER TABLE receipts ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE operations (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        amount_minor INTEGER,
        currency TEXT,
        operation_date TEXT,
        recipient TEXT,
        purpose TEXT,
        description TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE operation_links (
        id INTEGER PRIMARY KEY,
        receipt_id INTEGER NOT NULL REFERENCES receipts(id),
        operation_id INTEGER NOT NULL REFERENCES operations(id),
        basis TEXT NOT NULL,
        created_at TEXT NOT NULL,
        undone_at TEXT
    );
    CREATE INDEX receipts_operation ON receipts(operation_id);
    """,
}


# Синтетические документы не попадают в очередь, сводки, отчёты и интерфейс.
NOT_SYNTHETIC_DOCUMENT = "document_id NOT IN (SELECT id FROM documents WHERE is_synthetic = 1)"


class StorageError(AppError):
    """Ошибка хранилища; текст безопасен для вывода."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """Локальное хранилище SQLite. Пароли и ключи сюда не попадают."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        os.chmod(path, 0o600)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise StorageError("База создана более новой версией приложения")
        for version in sorted(MIGRATIONS):
            if version > current:
                self.conn.executescript(
                    f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
                )

    # Почта и источники

    def ensure_mailbox(self, account: str, name: str, uidvalidity: int) -> int:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO accounts (identifier) VALUES (?)", (account,))
            account_id = self.conn.execute("SELECT id FROM accounts WHERE identifier = ?", (account,)).fetchone()[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO mailboxes (account_id, name, uidvalidity) VALUES (?, ?, ?)",
                (account_id, name, uidvalidity),
            )
            return self.conn.execute(
                "SELECT id FROM mailboxes WHERE account_id = ? AND name = ? AND uidvalidity = ?",
                (account_id, name, uidvalidity),
            ).fetchone()[0]

    def ensure_message(self, mailbox_id: int, uid: int, received_date: str | None) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO messages (mailbox_id, uid, received_date, first_seen_at) VALUES (?, ?, ?, ?)",
                (mailbox_id, uid, received_date, utc_now()),
            )
            return self.conn.execute(
                "SELECT id FROM messages WHERE mailbox_id = ? AND uid = ?", (mailbox_id, uid)
            ).fetchone()[0]

    # Документы

    def document_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()

    def create_document(self, sha256: str, mime_type: str, size_bytes: int) -> int:
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO documents (sha256, mime_type, size_bytes, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (sha256, mime_type, size_bytes, now, now),
            )
        return int(cursor.lastrowid)

    def add_source(self, document_id: int, message_id: int | None, part_name: str) -> None:
        existing = self.conn.execute(
            "SELECT id FROM document_sources WHERE document_id = ? AND message_id IS ? AND part_name = ?",
            (document_id, message_id, part_name),
        ).fetchone()
        if existing:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO document_sources (document_id, message_id, part_name, first_seen_at) VALUES (?, ?, ?, ?)",
                (document_id, message_id, part_name, utc_now()),
            )

    def mark_synthetic(self, document_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE documents SET is_synthetic = 1, updated_at = ? WHERE id = ?", (utc_now(), document_id))

    def sources_for(self, document_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.part_name, s.message_id, m.uid, mb.name AS mailbox, mb.uidvalidity, a.identifier AS account "
            "FROM document_sources s "
            "LEFT JOIN messages m ON m.id = s.message_id "
            "LEFT JOIN mailboxes mb ON mb.id = m.mailbox_id "
            "LEFT JOIN accounts a ON a.id = mb.account_id "
            "WHERE s.document_id = ? ORDER BY s.id",
            (document_id,),
        ).fetchall()

    def set_document_text(
        self, document_id: int, text: str, complete: bool, page_count: int | None, status: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE documents SET extracted_text = ?, text_complete = ?, page_count = ?, status = ?, "
                "error_code = NULL, updated_at = ? WHERE id = ?",
                (text, int(complete), page_count, status, utc_now(), document_id),
            )

    def mark_text_incomplete(self, document_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE documents SET text_complete = 0, updated_at = ? WHERE id = ?", (utc_now(), document_id)
            )

    def set_document_status(self, document_id: int, status: str, error_code: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE documents SET status = ?, error_code = ?, updated_at = ? WHERE id = ?",
                (status, error_code, utc_now(), document_id),
            )

    # Попытки

    def record_attempt(
        self,
        document_id: int,
        *,
        stage: str,
        status: str,
        started_at: str,
        error_code: str | None = None,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        extractor_version: str,
        prompt_version: str | None = None,
        schema_version: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO processing_attempts (document_id, stage, status, error_code, model, usage_json, "
                "extractor_version, prompt_version, schema_version, started_at, finished_at, diagnostics_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    stage,
                    status,
                    error_code,
                    model,
                    json.dumps(usage, ensure_ascii=False) if usage else None,
                    extractor_version,
                    prompt_version,
                    schema_version,
                    started_at,
                    utc_now(),
                    json.dumps(diagnostics, ensure_ascii=False) if diagnostics else None,
                ),
            )
        return int(cursor.lastrowid)

    def attempts_for(self, document_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM processing_attempts WHERE document_id = ? ORDER BY id", (document_id,)
        ).fetchall()

    # Результаты и чеки

    def save_result(self, document_id: int, attempt_id: int | None, receipts: list[dict[str, Any]]) -> int:
        """Сохраняет новую версию результата. Подтверждённые и исправленные записи не заменяются."""
        now = utc_now()
        protected = self.conn.execute(
            "SELECT 1 FROM receipts WHERE document_id = ? AND is_current = 1 "
            "AND (review_status = 'confirmed' OR manual_edited = 1) LIMIT 1",
            (document_id,),
        ).fetchone()
        version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM extraction_results WHERE document_id = ?", (document_id,)
        ).fetchone()[0]
        with self.conn:
            if protected:
                is_current = 0
            else:
                is_current = 1
                self.conn.execute("UPDATE receipts SET is_current = 0 WHERE document_id = ? AND is_current = 1", (document_id,))
                self.conn.execute("UPDATE extraction_results SET is_current = 0 WHERE document_id = ?", (document_id,))
            cursor = self.conn.execute(
                "INSERT INTO extraction_results (document_id, attempt_id, version, is_current, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (document_id, attempt_id, version, is_current, now),
            )
            result_id = int(cursor.lastrowid)
            for item in receipts:
                warnings = list(item["warnings"])
                if protected:
                    warnings.append("Новая версия не заменила подтверждённую или исправленную запись")
                self.conn.execute(
                    "INSERT INTO receipts (document_id, result_id, ordinal, is_current, merchant_name, merchant_tax_id, "
                    "purchase_date, purchase_time, currency, amount_minor, amount_text, operation_type, category, "
                    "receipt_number, fiscal_id, evidence_json, warnings_json, review_status, manual_edited, "
                    "confirmed_with_partial_text, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (
                        document_id,
                        result_id,
                        item["ordinal"],
                        is_current,
                        item["merchant_name"],
                        item["merchant_tax_id"],
                        item["purchase_date"],
                        item["purchase_time"],
                        item["currency"],
                        item["amount_minor"],
                        item["amount_text"],
                        item["operation_type"],
                        item["category"],
                        item["receipt_number"],
                        item["fiscal_id"],
                        json.dumps(item["evidence"], ensure_ascii=False),
                        json.dumps(warnings, ensure_ascii=False),
                        item["review_status"],
                        now,
                        now,
                    ),
                )
                self.conn.execute(
                    "UPDATE receipts SET document_kind = ?, payment_status = ? WHERE id = last_insert_rowid()",
                    (item.get("document_kind", "unknown"), item.get("payment_status", "unknown")),
                )
                self.conn.execute(
                    "UPDATE receipts SET intermediary = ?, purpose = ?, description = ?, fee_minor = ?, debit_minor = ?, "
                    "identifiers_json = ? WHERE id = last_insert_rowid()",
                    (
                        item.get("intermediary"),
                        item.get("purpose"),
                        item.get("description"),
                        item.get("fee_minor"),
                        item.get("debit_minor"),
                        json.dumps(item["identifiers"], ensure_ascii=False) if item.get("identifiers") else None,
                    ),
                )
            self.conn.execute(
                "UPDATE documents SET status = 'ai_done', error_code = NULL, updated_at = ? WHERE id = ?",
                (now, document_id),
            )
        return result_id

    def current_receipts(self, document_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM receipts WHERE document_id = ? AND is_current = 1 ORDER BY ordinal", (document_id,)
        ).fetchall()

    def receipt(self, receipt_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()

    def update_receipt(self, receipt_id: int, changes: dict[str, Any]) -> list[tuple[str, Any, Any]]:
        """Меняет поля и пишет историю. changes: колонка -> новое значение (уже проверенное)."""
        if not set(changes) <= EDITABLE_COLUMNS:
            raise StorageError("Недопустимое поле для исправления")
        row = self.receipt(receipt_id)
        if row is None:
            raise StorageError("Чек с таким номером не найден")
        history = []
        now = utc_now()
        with self.conn:
            for column, new_value in changes.items():
                old_value = row[column]
                if old_value == new_value:
                    continue
                history.append((column, old_value, new_value))
                self.conn.execute(
                    "INSERT INTO receipt_corrections (receipt_id, field, old_value, new_value, changed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (receipt_id, column, None if old_value is None else str(old_value),
                     None if new_value is None else str(new_value), now),
                )
            if history:
                assignments = ", ".join(f"{column} = ?" for column, _, _ in history)
                self.conn.execute(
                    f"UPDATE receipts SET {assignments}, manual_edited = 1, updated_at = ? WHERE id = ?",
                    [value for _, _, value in history] + [now, receipt_id],
                )
        return history

    def set_review(self, receipt_id: int, status: str, *, partial_text: bool = False) -> None:
        if status not in REVIEW_STATUSES:
            raise StorageError("Недопустимый статус проверки")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "UPDATE receipts SET review_status = ?, confirmed_with_partial_text = ?, updated_at = ? WHERE id = ?",
                (status, int(partial_text), now, receipt_id),
            )
            if status == "confirmed":
                self.conn.execute(
                    "INSERT OR REPLACE INTO confirmed_expenses (receipt_id, confirmed_at) VALUES (?, ?)",
                    (receipt_id, now),
                )
            else:
                self.conn.execute("DELETE FROM confirmed_expenses WHERE receipt_id = ?", (receipt_id,))

    def corrections_for(self, receipt_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM receipt_corrections WHERE receipt_id = ? ORDER BY id", (receipt_id,)
        ).fetchall()

    def document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
