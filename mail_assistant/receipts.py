from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mail_assistant.config import (
    OPENCODE_GO_RECEIPTS_CONFIRMED,
    RECEIPT_RETRY_MAX_TOKENS,
    AppError,
    LLMSettings,
    ReceiptSettings,
)
from mail_assistant.documents import DocumentError, detect_kind, extract_document
from mail_assistant.llm_client import LLMClient, LLMError
from mail_assistant.money import (
    SUPPORTED_CURRENCIES,
    MoneyError,
    amount_to_minor,
    minor_to_text,
    normalize_currency,
    parse_amount,
)
from mail_assistant.ocr import OcrEngine
from mail_assistant.receipt_extract import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ReceiptExtractError,
    build_messages,
    looks_like_receipt,
    parse_receipt_batch,
    verify_receipts,
)
from mail_assistant.storage import CATEGORIES, OPERATION_TYPES, Store, StorageError, utc_now

EXTRACTOR_VERSION_NAME = "extractor-1"
EDITABLE_FIELDS = {
    "merchant": "merchant_name",
    "date": "purchase_date",
    "time": "purchase_time",
    "amount": "amount",
    "currency": "currency",
    "category": "category",
    "operation": "operation_type",
    "payment_status": "payment_status",
}
UNCERTAIN_CODES = frozenset({"LLM_UNCERTAIN"})
# Ошибки самого файла повторяются тем же результатом: без reprocess OCR заново не запускаем.
DETERMINISTIC_FAILURES = frozenset(
    {"FILE_TOO_LARGE", "UNSUPPORTED_TYPE", "FILE_CORRUPT", "PDF_ENCRYPTED", "TOO_MANY_PAGES", "IMAGE_TOO_LARGE", "NO_TEXT"}
)


class ReceiptError(AppError):
    """Ошибка работы с чеками; текст безопасен для вывода."""


@dataclass(frozen=True)
class SourceRef:
    part_name: str
    account: str | None = None
    mailbox: str | None = None
    uidvalidity: int | None = None
    uid: int | None = None


class LLMBudget:
    """Лимит числа платных LLM-запросов на один запуск."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def available(self) -> bool:
        return self.used < self.limit

    def consume(self) -> None:
        self.used += 1


@dataclass(frozen=True)
class AiOptions:
    allow_cloud: bool
    llm_settings: LLMSettings | None
    budget: LLMBudget
    client_factory: Callable[[LLMSettings], LLMClient] | None = None


@dataclass
class ProcessResult:
    document_id: int
    sha256: str
    status: str
    error_code: str | None = None
    receipt_ids: list[int] = field(default_factory=list)
    text_complete: bool = False
    duplicate: bool = False
    new_document: bool = False
    ai_called: bool = False
    notes: list[str] = field(default_factory=list)


def _llm_error_code(exc: LLMError) -> str:
    if exc.uncertain:
        return "LLM_UNCERTAIN"
    details = exc.details
    if details.get("finish_reason") == "length":
        return "LLM_TRUNCATED"
    if details.get("refusal") is True:
        return "LLM_REFUSED"
    return "LLM_FAILED"


def _store_original(settings: ReceiptSettings, sha256: str, data: bytes) -> None:
    """Оригинал только при RECEIPT_STORE_ORIGINALS=true; имя файла — SHA-256, а не имя вложения."""
    import os

    directory = settings.originals_dir
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = directory / f"{sha256}.bin"
    if target.exists():
        return
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _current_ids(store: Store, document_id: int) -> list[int]:
    return [int(row["id"]) for row in store.current_receipts(document_id)]


def process_document(
    store: Store,
    data: bytes,
    *,
    source: SourceRef,
    settings: ReceiptSettings,
    ocr: OcrEngine,
    ai: AiOptions | None = None,
    reprocess: bool = False,
) -> ProcessResult:
    sha256 = hashlib.sha256(data).hexdigest()
    existing = store.document_by_sha(sha256)
    if existing is None:
        try:
            _, mime = detect_kind(data)
        except DocumentError:
            mime = "application/octet-stream"
        document_id = store.create_document(sha256, mime, len(data))
    else:
        document_id = int(existing["id"])
    is_new = existing is None

    message_id = None
    if source.account and source.mailbox and source.uidvalidity is not None and source.uid is not None:
        mailbox_id = store.ensure_mailbox(source.account, source.mailbox, source.uidvalidity)
        message_id = store.ensure_message(mailbox_id, source.uid, None)
    store.add_source(document_id, message_id, source.part_name)
    if settings.store_originals and len(data) <= settings.max_bytes:
        _store_original(settings, sha256, data)

    document = store.document(document_id)
    result = ProcessResult(
        document_id=document_id, sha256=sha256, status=document["status"], new_document=is_new, duplicate=not is_new
    )
    if len(data) > settings.max_bytes:
        store.set_document_status(document_id, "failed", "FILE_TOO_LARGE")
        result.status, result.error_code = "failed", "FILE_TOO_LARGE"
        return result

    if document["status"] == "ai_done" and not reprocess:
        result.status = "ai_done"
        result.receipt_ids = _current_ids(store, document_id)
        result.text_complete = bool(document["text_complete"])
        result.notes.append("Документ уже обработан: повторный запрос к AI не выполнялся")
        return result

    if document["status"] == "failed" and document["error_code"] in DETERMINISTIC_FAILURES and not reprocess:
        result.error_code = document["error_code"]
        result.notes.append("Документ уже проверен и не содержит пригодного текста: OCR не запускался повторно")
        return result

    text_complete: bool
    text: str
    if document["extracted_text"] and not reprocess:
        text = document["extracted_text"]
        text_complete = bool(document["text_complete"])
        result.notes.append("Текст взят из базы: OCR повторно не запускался")
    else:
        started = utc_now()
        try:
            extracted = extract_document(data, settings, ocr)
        except DocumentError as exc:
            store.record_attempt(
                document_id, stage="extract", status="failed", error_code=exc.code,
                started_at=started, extractor_version=EXTRACTOR_VERSION_NAME,
            )
            store.set_document_status(document_id, "failed", exc.code)
            result.status, result.error_code = "failed", exc.code
            return result
        text = extracted.combined_text()
        text_complete = True
        store.set_document_text(document_id, text, True, extracted.page_count, "text_extracted")
        store.record_attempt(
            document_id, stage="extract", status="ok", started_at=started,
            extractor_version=EXTRACTOR_VERSION_NAME,
        )
    result.status = "text_extracted"
    result.text_complete = text_complete

    if ai is None or not ai.allow_cloud:
        result.notes.append("LLM не вызывался: нужен --allow-cloud")
        return result
    return _run_ai(store, document_id, text, text_complete, ai, result, reprocess, document)


def _run_ai(
    store: Store,
    document_id: int,
    text: str,
    text_complete: bool,
    ai: AiOptions,
    result: ProcessResult,
    reprocess: bool,
    document: Any,
) -> ProcessResult:
    llm = ai.llm_settings
    if llm is None or not llm.email_use_confirmed:
        result.error_code = "LLM_NOT_CONFIRMED"
        result.notes.append("Облачный анализ заблокирован: LLM_EMAIL_USE_CONFIRMED не равен true")
        return result
    if not OPENCODE_GO_RECEIPTS_CONFIRMED and ai.client_factory is None:
        result.error_code = "PROVIDER_NOT_CONFIRMED"
        result.notes.append(
            "Отправка чеков в облачный API не подтверждена условиями провайдера "
            "(OPENCODE_GO_RECEIPTS_CONFIRMED = False)"
        )
        return result
    if not looks_like_receipt(text):
        store.set_document_status(document_id, "text_extracted", "NOT_RECEIPT")
        result.status, result.error_code = "text_extracted", "NOT_RECEIPT"
        result.notes.append("В тексте нет признаков покупки: в LLM не отправлено")
        return result
    if document["error_code"] in UNCERTAIN_CODES and not reprocess:
        result.status, result.error_code = "text_extracted", "LLM_UNCERTAIN"
        result.notes.append("Предыдущий запрос завершился неопределённо: повтор только через reprocess")
        return result
    if not ai.budget.available():
        result.error_code = "BUDGET_EXHAUSTED"
        result.notes.append("Лимит LLM-запросов на запуск исчерпан: документ оставлен в очереди")
        return result

    payload = build_messages(text, llm.max_input_chars)
    complete = text_complete and not payload.truncated
    if payload.truncated:
        store.mark_text_incomplete(document_id)
    model = llm.model
    started = utc_now()
    factory = ai.client_factory or LLMClient

    ai.budget.consume()
    result.ai_called = True
    try:
        with factory(llm) as client:
            chat = client.chat(payload.messages, model)
    except LLMError as exc:
        can_retry = (
            _llm_error_code(exc) == "LLM_TRUNCATED"
            and llm.max_output_tokens < RECEIPT_RETRY_MAX_TOKENS  # повтор без большего бюджета бессмыслен
            and ai.budget.available()
        )
        if not can_retry:
            return _record_llm_failure(store, document_id, exc, model, started, result)
        # Один повтор только при явном finish_reason=length, с бюджетом вывода 4096.
        # Первый обрезанный вызов фиксируется отдельно: он тоже потратил запрос.
        store.record_attempt(
            document_id, stage="llm", status="failed", error_code="LLM_TRUNCATED", model=model, usage=exc.details.get("usage"),
            started_at=started, extractor_version=EXTRACTOR_VERSION_NAME, prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION, diagnostics=exc.details,
        )
        ai.budget.consume()
        result.notes.append("Ответ обрезан по лимиту: один повтор с бюджетом вывода 4096 токенов")
        try:
            with factory(llm) as client:
                chat = client.chat(payload.messages, model, max_tokens=RECEIPT_RETRY_MAX_TOKENS)
        except LLMError as retry_exc:
            return _record_llm_failure(store, document_id, retry_exc, model, started, result)

    try:
        batch = parse_receipt_batch(chat.content, chat.diagnostics)
    except ReceiptExtractError as exc:
        store.record_attempt(
            document_id, stage="llm", status="failed", error_code=exc.code, model=chat.reported_model or model,
            usage=chat.usage, started_at=started, extractor_version=EXTRACTOR_VERSION_NAME,
            prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION, diagnostics=exc.details,
        )
        store.set_document_status(document_id, "text_extracted", exc.code)
        result.status, result.error_code = "text_extracted", exc.code
        return result

    rows = verify_receipts(batch, text, complete)
    attempt_id = store.record_attempt(
        document_id, stage="llm", status="ok", model=chat.reported_model or model, usage=chat.usage,
        started_at=started, extractor_version=EXTRACTOR_VERSION_NAME,
        prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
    )
    store.save_result(document_id, attempt_id, rows)
    result.status = "ai_done"
    result.text_complete = complete
    result.receipt_ids = _current_ids(store, document_id)
    return result


def _record_llm_failure(
    store: Store, document_id: int, exc: LLMError, model: str, started: str, result: ProcessResult
) -> ProcessResult:
    code = _llm_error_code(exc)
    store.record_attempt(
        document_id, stage="llm", status="failed", error_code=code, model=model, started_at=started,
        extractor_version=EXTRACTOR_VERSION_NAME, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
        usage=exc.details.get("usage"), diagnostics=exc.details,
    )
    store.set_document_status(document_id, "text_extracted", code)
    result.status, result.error_code = "text_extracted", code
    return result


def analyze_document_text(
    store: Store, document_id: int, ai: AiOptions, *, retry_uncertain: bool = False
) -> ProcessResult:
    """AI-анализ уже извлечённого текста: без повторного OCR и без чтения почты."""
    document = store.document(document_id)
    if document is None:
        raise ReceiptError("Документ не найден")
    if not document["extracted_text"]:
        raise ReceiptError("У документа нет извлечённого текста: сначала извлеките текст")
    result = ProcessResult(
        document_id=document_id, sha256=document["sha256"], status=document["status"], duplicate=True
    )
    if document["status"] == "ai_done":
        result.status = "ai_done"
        result.receipt_ids = _current_ids(store, document_id)
        result.text_complete = bool(document["text_complete"])
        return result
    result.text_complete = bool(document["text_complete"])
    if not ai.allow_cloud:
        result.error_code = "LLM_NOT_CONFIRMED"
        return result
    return _run_ai(
        store, document_id, document["extracted_text"], bool(document["text_complete"]), ai, result,
        retry_uncertain, document,
    )


# Ручная проверка

def receipt_view(store: Store, receipt_id: int) -> dict[str, Any]:
    row = store.receipt(receipt_id)
    if row is None:
        raise ReceiptError("Чек с таким номером не найден")
    return dict(row)


def confirm_receipt(store: Store, receipt_id: int) -> bool:
    """Подтверждает запись. Возвращает True, если текст документа был неполным (отметка сохраняется)."""
    row = store.receipt(receipt_id)
    if row is None:
        raise ReceiptError("Чек с таким номером не найден")
    if not row["is_current"]:
        raise ReceiptError("Нельзя подтвердить неактуальную версию результата")
    if row["review_status"] == "rejected":
        raise ReceiptError("Отклонённую запись нельзя подтвердить; сначала восстановите её через исправление")
    if row["payment_status"] == "unpaid" or (row["document_kind"] == "invoice" and row["payment_status"] != "paid"):
        raise ReceiptError("Это счёт, а не подтверждённая оплата. Если он уже оплачен, укажите «Оплачен» и сохраните исправления")
    missing = [
        label
        for label, value in (
            ("магазин", row["merchant_name"]),
            ("дата", row["purchase_date"]),
            ("валюта", row["currency"]),
            ("сумма", row["amount_minor"]),
        )
        if value is None
    ]
    if row["operation_type"] not in ("expense", "refund"):
        missing.append("тип операции (расход/возврат)")
    if missing:
        raise ReceiptError("Нельзя подтвердить: не заполнено: " + ", ".join(missing))
    document = store.document(row["document_id"])
    partial = not bool(document["text_complete"])
    store.set_review(receipt_id, "confirmed", partial_text=partial)
    return partial


def reject_receipt(store: Store, receipt_id: int) -> None:
    if store.receipt(receipt_id) is None:
        raise ReceiptError("Чек с таким номером не найден")
    store.set_review(receipt_id, "rejected")


def edit_receipt(store: Store, receipt_id: int, changes: dict[str, str]) -> list[tuple[str, Any, Any]]:
    """Ручное исправление. changes: ключи из EDITABLE_FIELDS. Значения проверяются перед записью."""
    row = store.receipt(receipt_id)
    if row is None:
        raise ReceiptError("Чек с таким номером не найден")
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ReceiptError("Неизвестное поле: " + ", ".join(sorted(unknown)))
    updates: dict[str, Any] = {}
    try:
        for key, raw in changes.items():
            if key == "merchant":
                value = raw.strip()
                if not value or len(value) > 200:
                    raise ReceiptError("Магазин: от 1 до 200 символов")
                updates["merchant_name"] = value
            elif key == "date":
                try:
                    date.fromisoformat(raw)
                except ValueError:
                    raise ReceiptError("Дата должна быть в формате YYYY-MM-DD") from None
                updates["purchase_date"] = raw
            elif key == "time":
                if raw.lower() in ("none", "-"):
                    updates["purchase_time"] = None
                elif len(raw) in (5, 8) and raw[2] == ":":
                    updates["purchase_time"] = raw
                else:
                    raise ReceiptError("Время в формате HH:MM или HH:MM:SS")
            elif key == "currency":
                normalized = normalize_currency(raw.upper())
                if normalized not in SUPPORTED_CURRENCIES:
                    raise ReceiptError("Валюта не поддерживается: " + ", ".join(sorted(SUPPORTED_CURRENCIES)))
                updates["currency"] = normalized
            elif key == "category":
                if raw not in CATEGORIES:
                    raise ReceiptError("Категория: " + ", ".join(CATEGORIES))
                updates["category"] = raw
            elif key == "operation":
                if raw not in OPERATION_TYPES:
                    raise ReceiptError("Тип операции: expense, refund или unknown")
                updates["operation_type"] = raw
            elif key == "payment_status":
                if raw not in ("paid", "unpaid", "unknown"):
                    raise ReceiptError("Статус оплаты: paid, unpaid или unknown")
                updates["payment_status"] = raw
            elif key == "amount":
                value = parse_amount(raw)
                if value <= 0:
                    raise ReceiptError("Сумма должна быть больше нуля")
                updates["amount_text"] = f"{value:.2f}"
                updates["_amount"] = value
    except MoneyError as exc:
        raise ReceiptError(str(exc)) from None

    amount_value = updates.pop("_amount", None)
    currency = updates.get("currency", row["currency"])
    if amount_value is not None:
        updates["amount_minor"] = amount_to_minor(amount_value) if currency else None
        # Ручная сумма — подтверждённый итог и отток одновременно: отчёт считает отток.
        updates["debit_minor"] = updates["amount_minor"]
    elif "currency" in updates and row["amount_text"] is not None:
        value = parse_amount(row["amount_text"])
        updates["amount_minor"] = amount_to_minor(value) if currency else None
    try:
        return store.update_receipt(receipt_id, updates)
    except StorageError as exc:
        raise ReceiptError(str(exc)) from None


def review_queue(store: Store) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT r.*, d.text_complete FROM receipts r JOIN documents d ON d.id = r.document_id "
        "WHERE r.is_current = 1 AND r.review_status IN ('needs_review', 'extracted') "
        "ORDER BY r.purchase_date IS NULL, r.purchase_date, r.id"
    ).fetchall()
    return [dict(row) for row in rows]


def duplicate_suspects(store: Store) -> list[dict[str, Any]]:
    """Подозрение на дубль: одинаковые магазин, дата, сумма и валюта в разных документах. Не удаляет ничего."""
    rows = store.conn.execute(
        "SELECT merchant_name, purchase_date, currency, amount_minor, COUNT(DISTINCT document_id) AS documents, "
        "GROUP_CONCAT(id) AS receipt_ids FROM receipts "
        "WHERE is_current = 1 AND merchant_name IS NOT NULL AND purchase_date IS NOT NULL AND amount_minor IS NOT NULL "
        "AND review_status != 'rejected' "
        "GROUP BY merchant_name, purchase_date, currency, amount_minor HAVING COUNT(DISTINCT document_id) > 1"
    ).fetchall()
    return [dict(row) for row in rows]


def format_amount(minor: int | None, currency: str | None) -> str:
    if minor is None:
        return "—"
    return f"{minor_to_text(minor)} {currency or ''}".strip()


__all__ = [
    "AiOptions",
    "LLMBudget",
    "ProcessResult",
    "ReceiptError",
    "SourceRef",
    "confirm_receipt",
    "duplicate_suspects",
    "edit_receipt",
    "format_amount",
    "process_document",
    "receipt_view",
    "reject_receipt",
    "review_queue",
]
