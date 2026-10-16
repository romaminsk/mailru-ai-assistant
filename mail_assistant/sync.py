from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from email import policy
from email.parser import BytesParser

from mail_assistant.config import AppError, ReceiptSettings
from mail_assistant.bodystructure import Attachment, candidate_attachments, matches_kind
from mail_assistant.imap_client import ImapSession, MailError
from mail_assistant.ocr import OcrEngine
from mail_assistant.parser import sanitize_for_terminal
from mail_assistant.receipts import AiOptions, ProcessResult, SourceRef, process_document
from mail_assistant.storage import Store

MAX_PERIOD_DAYS = 92
MAX_BATCH = 50
CANDIDATE_TYPES = ("application/pdf", "image/png")
CANDIDATE_SUFFIXES = (".pdf", ".png")


class SyncError(AppError):
    """Ошибка синхронизации; текст безопасен для вывода."""


@dataclass(frozen=True)
class SyncPlan:
    mailbox_id: str
    uidvalidity: int
    window_messages: int
    candidate_messages: int
    unknown_structure: int
    selected_uids: tuple[int, ...]
    upper_bound_bytes: int


@dataclass
class SyncStats:
    messages: int = 0
    documents: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    ai_calls: int = 0
    budget_skipped: int = 0
    message_errors: int = 0


def validate_period(received_from: date, received_to: date) -> None:
    if received_from > received_to:
        raise SyncError("Дата начала позже даты окончания")
    if (received_to - received_from).days + 1 > MAX_PERIOD_DAYS:
        raise SyncError(f"Период больше {MAX_PERIOD_DAYS} дней; разбейте запуск")


def validate_batch(max_messages: int) -> None:
    if not 1 <= max_messages <= MAX_BATCH:
        raise SyncError(f"--max-messages должен быть от 1 до {MAX_BATCH}")


def plan_sync(
    session: ImapSession, received_from: date, received_to: date, max_messages: int, kind: str = "all",
    min_bytes: int = 0,
) -> SyncPlan:
    """Отбор по BODYSTRUCTURE: тела писем и вложения не загружаются.

    Письма без PNG/PDF по структуре пропускаются. Письма с неразобранной структурой остаются
    кандидатами и проверяются полностью при реальном запуске.
    """
    info, _ = session.open_mailbox()
    if session.uidvalidity is None:
        raise SyncError("Сервер не вернул UIDVALIDITY для папки; синхронизация остановлена")
    uids = session.search_received(received_from, received_to)
    structures = session.fetch_structures(uids)
    candidates: list[tuple[int, int]] = []
    unknown = 0
    for uid in uids:
        meta = structures.get(uid)
        found = candidate_attachments(meta, kind, min_bytes) if meta is not None else None
        if found is None:
            unknown += 1
            candidates.append((uid, 0))
        elif found:
            candidates.append((uid, sum(item.size for item in found)))
    selected = candidates[-max_messages:]
    return SyncPlan(
        mailbox_id=info.mailbox_id,
        uidvalidity=session.uidvalidity,
        window_messages=len(uids),
        candidate_messages=len(candidates),
        unknown_structure=unknown,
        selected_uids=tuple(uid for uid, _ in selected),
        upper_bound_bytes=sum(size for _, size in selected),
    )


def candidate_parts(raw: bytes, kind: str = "all", min_bytes: int = 0) -> list[tuple[int, str, bytes]]:
    """PNG/PDF части письма. Части не открываются как программы и не сохраняются на диск."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    found: list[tuple[int, str, bytes]] = []
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        if not matches_kind(Attachment(part.get_content_type(), name, 0), kind):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload and len(payload) >= min_bytes:
            found.append((index, sanitize_for_terminal(name)[:80], payload))
    return found


def run_sync(
    session: ImapSession,
    store: Store,
    account: str,
    plan: SyncPlan,
    settings: ReceiptSettings,
    ocr: OcrEngine,
    ai: AiOptions,
    max_documents: int = 5,
    kind: str = "all",
    min_bytes: int = 0,
) -> SyncStats:
    stats = SyncStats()
    new_documents = 0
    for uid in plan.selected_uids:
        try:
            raw = session.fetch_message(uid)
            parts = candidate_parts(raw, kind, min_bytes)
        except MailError:
            stats.message_errors += 1
            continue
        stats.messages += 1
        for index, name, payload in parts:
            known = store.document_by_sha(hashlib.sha256(payload).hexdigest()) is not None
            if not known and new_documents >= max_documents:
                stats.documents["отложено (лимит --max-documents)"] += 1
                continue
            source = SourceRef(
                part_name=f"{index}:{name}",
                account=account,
                mailbox=plan.mailbox_id,
                uidvalidity=plan.uidvalidity,
                uid=uid,
            )
            result = process_document(store, payload, source=source, settings=settings, ocr=ocr, ai=ai)
            if result.new_document:
                new_documents += 1
            _count(stats, result)
    stats.ai_calls = ai.budget.used
    return stats


def _count(stats: SyncStats, result: ProcessResult) -> None:
    if result.duplicate:
        stats.documents["уже известен (повтор из другого письма)"] += 1
    else:
        stats.documents[result.status] += 1
    if result.error_code:
        stats.errors[result.error_code] += 1
    if result.error_code == "BUDGET_EXHAUSTED":
        stats.budget_skipped += 1


def describe_plan(plan: SyncPlan, max_messages: int) -> list[str]:
    megabytes = plan.upper_bound_bytes / (1024 * 1024)
    lines = [
        f"Папка: {plan.mailbox_id} (UIDVALIDITY {plan.uidvalidity})",
        f"Писем в периоде по дате получения: {plan.window_messages}",
        f"Писем с PNG/PDF-вложениями (по BODYSTRUCTURE): {plan.candidate_messages - plan.unknown_structure}",
    ]
    if plan.unknown_structure:
        lines.append(f"Писем с нераспознанной структурой (проверятся целиком): {plan.unknown_structure}")
    lines.append(f"Будет обработано (лимит --max-messages {max_messages}): {len(plan.selected_uids)}")
    lines.append(
        f"Размер PNG/PDF-вложений в них по структуре: {megabytes:.2f} МБ "
        "(тела писем не загружались; LLM не вызывается)"
    )
    return lines


def describe_stats(stats: SyncStats) -> list[str]:
    lines = [f"Просмотрено писем: {stats.messages}; ошибок чтения писем: {stats.message_errors}"]
    for status, count in sorted(stats.documents.items()):
        lines.append(f"Документы, {status}: {count}")
    for code, count in sorted(stats.errors.items()):
        lines.append(f"Ошибка {code}: {count}")
    lines.append(f"LLM-запросов в этом запуске: {stats.ai_calls}")
    if stats.budget_skipped:
        lines.append(f"Отложено из-за лимита LLM-запросов: {stats.budget_skipped}")
    return lines
