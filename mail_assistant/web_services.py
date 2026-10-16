"""Сервисный слой интерфейса: без Streamlit, чтобы его можно было тестировать напрямую.

Все действия вызываются только по кнопкам интерфейса. Перерисовка страницы ничего не запускает.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from mail_assistant.config import (
    OPENCODE_GO_RECEIPTS_CONFIRMED,
    AppError,
    ReceiptSettings,
    load_llm_settings,
    load_mail_settings,
    load_receipt_settings,
)
from mail_assistant.imap_client import ImapSession, MailboxInfo
from mail_assistant.ocr import TesseractOcr
from mail_assistant.receipts import (
    AiOptions,
    LLMBudget,
    ProcessResult,
    SourceRef,
    analyze_document_text,
    process_document,
)
from mail_assistant.report import build_report
from mail_assistant.storage import Store
from mail_assistant.sync import SyncStats, describe_plan, describe_stats, plan_sync, run_sync

# Модели, которые OpenCode Go перечисляет с endpoint Chat Completions (openai/v1/chat/completions).
# Список взят из документации, а не запрошен у сервера; реальную доступность проверяет сам API.
COMPATIBLE_MODELS = (
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.7-code",
    "longcat-2.0",
    "mimo-v2.6-flash",
    "mimo-v2.6-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "hy3",
    "space-bunny",
)
KIND_CHOICES = {"Все (PNG и PDF)": "all", "Только PNG": "png", "Только PDF": "pdf"}
MAX_RECOGNIZE_PER_CLICK = 3
LLM_STAGE_LIMIT = 100  # общий потолок облачных запросов этапа, включая повторы; уже сделанные учитываются


@dataclass(frozen=True)
class CloudStatus:
    allowed: bool
    reason: str


def ui_receipt_settings() -> ReceiptSettings:
    """Во время импорта исходный чек сохраняется в приватной data/ для проверки распознавания."""
    return replace(load_receipt_settings(), store_originals=True)


def open_store() -> Store:
    return Store(load_receipt_settings().db_path)


def cloud_status() -> CloudStatus:
    if not OPENCODE_GO_RECEIPTS_CONFIRMED:
        return CloudStatus(False, "Облачный AI отключён: условия провайдера для чеков не подтверждены")
    try:
        llm = load_llm_settings()
    except AppError as exc:
        return CloudStatus(False, str(exc))
    if not llm.email_use_confirmed:
        return CloudStatus(False, "LLM_EMAIL_USE_CONFIRMED не равен true")
    return CloudStatus(True, "Облачный AI доступен")


def list_folders() -> list[MailboxInfo]:
    with ImapSession(load_mail_settings()) as session:
        return session.list_mailboxes()


def import_mailbox(
    mailbox: str, received_from: date, received_to: date, max_messages: int, kind: str, max_documents: int
) -> tuple[list[str], SyncStats]:
    """Загрузка вложений из папки в режиме только чтения; LLM не вызывается."""
    mail = load_mail_settings()
    settings = ui_receipt_settings()
    ocr = TesseractOcr(settings.ocr_languages, settings.ocr_timeout_seconds)
    ai = AiOptions(allow_cloud=False, llm_settings=None, budget=LLMBudget(0))
    with Store(settings.db_path) as store, ImapSession(mail, mailbox) as session:
        plan = plan_sync(session, received_from, received_to, max_messages, kind)
        stats = run_sync(
            session, store, mail.username, plan, settings, ocr, ai, max_documents=max_documents, kind=kind
        )
    return describe_plan(plan, max_messages) + describe_stats(stats), stats


def import_uploads(files: list[tuple[str, bytes]]) -> list[ProcessResult]:
    """Загруженные PNG/PDF: локальный текст, без облака."""
    settings = ui_receipt_settings()
    ocr = TesseractOcr(settings.ocr_languages, settings.ocr_timeout_seconds)
    ai = AiOptions(allow_cloud=False, llm_settings=None, budget=LLMBudget(0))
    results = []
    with Store(settings.db_path) as store:
        for name, data in files:
            if len(data) > settings.max_bytes:
                raise AppError(f"Файл больше RECEIPT_MAX_BYTES: {name[:80]}")
            results.append(
                process_document(
                    store, data, source=SourceRef(part_name=name[:120]), settings=settings, ocr=ocr, ai=ai
                )
            )
    return results


def pending_documents(store: Store) -> list[dict[str, Any]]:
    """Документы с извлечённым текстом, по которым ещё нет записей чеков."""
    rows = store.conn.execute(
        "SELECT d.id, d.sha256, d.status, d.error_code, d.page_count, d.text_complete, "
        "length(d.extracted_text) AS chars FROM documents d "
        "WHERE d.extracted_text IS NOT NULL AND d.status != 'ai_done' "
        "AND COALESCE(d.error_code, '') NOT IN ('NOT_RECEIPT', 'NO_TEXT', 'LLM_UNCERTAIN') "
        "AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.document_id = d.id AND r.is_current = 1) "
        "ORDER BY d.id"
    ).fetchall()
    from mail_assistant.receipt_extract import looks_like_receipt

    return [dict(row) for row in rows if looks_like_receipt(store.document(row['id'])['extracted_text'])]


def recognize_documents(
    store: Store, model: str, document_ids: list[int], *, retry_uncertain: bool = False
) -> list[ProcessResult]:
    """AI-анализ выбранных документов. Выполняется только по кнопке и только при подтверждённом облаке."""
    status = cloud_status()
    if not status.allowed:
        raise AppError(status.reason)
    if model not in COMPATIBLE_MODELS:
        raise AppError("Модель не входит в список совместимых с Chat Completions")
    llm = load_llm_settings(model)
    ids = document_ids[:MAX_RECOGNIZE_PER_CLICK]
    used = store.conn.execute("SELECT COUNT(*) FROM processing_attempts WHERE stage = 'llm'").fetchone()[0]
    remaining = LLM_STAGE_LIMIT - used
    if remaining <= 0:
        raise AppError(f"Лимит {LLM_STAGE_LIMIT} LLM-запросов на этап исчерпан")
    # На каждый документ: один запрос и не более одного повтора при length.
    ai = AiOptions(allow_cloud=True, llm_settings=llm, budget=LLMBudget(min(remaining, 2 * len(ids))))
    # retry_uncertain: явный повтор после таймаута/сети/5xx с неизвестным исходом (возможен второй расход).
    return [analyze_document_text(store, document_id, ai, retry_uncertain=retry_uncertain) for document_id in ids]


def receipt_rows(store: Store) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT r.*, d.sha256, d.status AS document_status, d.error_code AS document_error "
        "FROM receipts r JOIN documents d ON d.id = r.document_id "
        "WHERE r.is_current = 1 ORDER BY r.purchase_date IS NULL, r.purchase_date, r.id"
    ).fetchall()
    return [dict(row) for row in rows]


def sources_summary(store: Store, document_id: int) -> str:
    parts = []
    for row in store.sources_for(document_id):
        if row["uid"] is None:
            parts.append(f"файл: {row['part_name'][:60]}")
        else:
            parts.append(f"папка {row['mailbox']}, UID {row['uid']}")
    return "; ".join(parts) or "—"


def original_path(store: Store, document_id: int) -> tuple[str, bytes | None]:
    document = store.document(document_id)
    if document is None:
        return "application/octet-stream", None
    path = load_receipt_settings().originals_dir / f"{document['sha256']}.bin"
    if not path.exists():
        return document["mime_type"], None
    return document["mime_type"], path.read_bytes()


def report_with_pending(store: Store, date_from: str, date_to: str) -> dict[str, Any]:
    report = build_report(store, date_from, date_to)
    report["pending_rows"] = [
        dict(row)
        for row in store.conn.execute(
            "SELECT id, purchase_date, merchant_name, currency, amount_minor, operation_type, category, review_status "
            "FROM receipts WHERE is_current = 1 AND review_status IN ('extracted', 'needs_review') "
            "AND purchase_date BETWEEN ? AND ? ORDER BY purchase_date, id",
            (date_from, date_to),
        ).fetchall()
    ]
    return report


def preferred_model(store: Store) -> str:
    """Keep using the last successful compatible model instead of resetting to DeepSeek."""
    row = store.conn.execute(
        "SELECT model FROM processing_attempts WHERE stage = 'llm' AND status = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and row['model'] in COMPATIBLE_MODELS:
        return row['model']
    try:
        configured = load_llm_settings().model
        return configured if configured in COMPATIBLE_MODELS else COMPATIBLE_MODELS[0]
    except AppError:
        return COMPATIBLE_MODELS[0]
