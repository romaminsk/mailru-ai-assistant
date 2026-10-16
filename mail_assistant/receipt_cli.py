from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from mail_assistant.config import AppError, load_llm_settings, load_mail_settings, load_receipt_settings
from mail_assistant.imap_client import ImapSession, MailError, validate_mailbox_id
from mail_assistant.ocr import TesseractOcr
from mail_assistant.parser import sanitize_for_terminal
from mail_assistant.receipts import (
    AiOptions,
    EDITABLE_FIELDS,
    LLMBudget,
    ReceiptError,
    SourceRef,
    confirm_receipt,
    duplicate_suspects,
    edit_receipt,
    format_amount,
    process_document,
    receipt_view,
    reject_receipt,
    review_queue,
)
from mail_assistant.report import build_report, parse_period, render_report, write_csv
from mail_assistant.storage import Store
from mail_assistant.sync import (
    SyncError,
    describe_plan,
    describe_stats,
    plan_sync,
    run_sync,
    validate_batch,
    validate_period,
)

ERROR_HINTS = {
    "FILE_TOO_LARGE": "файл больше RECEIPT_MAX_BYTES",
    "UNSUPPORTED_TYPE": "поддерживаются только PNG и PDF (по содержимому файла)",
    "FILE_CORRUPT": "файл повреждён или не читается",
    "PDF_ENCRYPTED": "PDF под паролем; расшифровка не выполняется",
    "TOO_MANY_PAGES": "страниц больше RECEIPT_MAX_PAGES",
    "IMAGE_TOO_LARGE": "изображение больше RECEIPT_MAX_PIXELS",
    "NO_TEXT": "текст не найден ни встроенным слоем, ни OCR",
    "OCR_UNAVAILABLE": "Tesseract не найден: установите его (см. README, раздел OCR)",
    "OCR_LANG_MISSING": "не установлены языки rus/eng для Tesseract (см. README, раздел OCR)",
    "OCR_TIMEOUT": "OCR превысил RECEIPT_OCR_TIMEOUT_SECONDS",
    "OCR_FAILED": "ошибка OCR",
    "LLM_NOT_CONFIRMED": "облачный анализ не разрешён: LLM_EMAIL_USE_CONFIRMED должен быть true",
    "LLM_INVALID": "ответ модели не прошёл проверку; результат не сохранён",
    "LLM_FAILED": "запрос к LLM не выполнен; автоматического повтора нет",
    "LLM_REFUSED": "модель отказалась отвечать",
    "LLM_TRUNCATED": "ответ модели обрезан лимитом; автоматического повтора нет",
    "LLM_UNCERTAIN": "предыдущий платный запрос завершился неопределённо; повтор только через --reprocess",
    "BUDGET_EXHAUSTED": "лимит LLM-запросов на запуск исчерпан",
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_LOCAL_NAME = 120


def _mailbox_arg(value: str) -> str:
    try:
        return validate_mailbox_id(value)
    except MailError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _iso_date(value: str) -> date:
    if not _DATE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("дата в формате YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("некорректная дата") from None


def add_receipt_parsers(commands: Any) -> None:
    receipt = commands.add_parser("receipt", help="чеки: извлечение, проверка и исправление")
    sub = receipt.add_subparsers(dest="receipt_command", required=True)

    extract = sub.add_parser("extract", help="обработать один локальный PNG или PDF")
    extract.add_argument("path")
    extract.add_argument("--allow-cloud", action="store_true", help="отправить распознанный текст в LLM")
    extract.add_argument("--model", default=None)
    extract.add_argument("--reprocess", action="store_true", help="повторная обработка (платный AI-запрос)")
    extract.add_argument("--show-text", action="store_true", help="показать распознанный текст (отладка)")

    list_cmd = sub.add_parser("list", help="список чеков")
    list_cmd.add_argument("--limit", type=int, default=50)

    show = sub.add_parser("show", help="показать чек")
    show.add_argument("id", type=int)
    show.add_argument("--text", action="store_true", help="показать текст документа (отладка)")

    sub.add_parser("review", help="записи на проверку и подозрения на дубли")

    backup = sub.add_parser("backup", help="резервная копия базы (SQLite backup API)")
    backup.add_argument("--out", default=None, help="путь к файлу; по умолчанию data/backups/")

    edit = sub.add_parser("edit", help="исправить поля чека вручную")
    edit.add_argument("id", type=int)
    for key in EDITABLE_FIELDS:
        edit.add_argument(f"--{key}", default=None)

    confirm = sub.add_parser("confirm", help="подтвердить расход")
    confirm.add_argument("id", type=int)
    reject = sub.add_parser("reject", help="отклонить запись")
    reject.add_argument("id", type=int)

    report = commands.add_parser("report", help="отчёт за период по дате покупки (без IMAP и LLM)")
    report.add_argument("--from", dest="date_from", required=True, type=_iso_date)
    report.add_argument("--to", dest="date_to", required=True, type=_iso_date)
    report.add_argument("--csv", default=None, help="путь к CSV-файлу")
    report.add_argument("--force", action="store_true", help="перезаписать CSV")

    sync = commands.add_parser("sync", help="чеки-вложения из папки почты (dry-run по умолчанию безопасен)")
    sync.add_argument("--mailbox", required=True, type=_mailbox_arg, help="идентификатор папки из 'mail folders' (обязателен)")
    sync.add_argument("--received-from", required=True, type=_iso_date, help="дата получения письма, включительно")
    sync.add_argument("--received-to", required=True, type=_iso_date, help="дата получения письма, включительно")
    sync.add_argument("--max-messages", required=True, type=int)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--allow-cloud", action="store_true")
    sync.add_argument("--local-only", action="store_true", help="только локальный OCR/текст, без LLM")
    sync.add_argument("--max-documents", type=int, default=5, help="лимит новых документов за запуск (1..20)")
    sync.add_argument("--kinds", choices=("pdf", "png", "all"), default="all", help="типы вложений")
    sync.add_argument("--min-bytes", type=int, default=0, help="пропускать вложения меньше этого размера (иконки)")
    sync.add_argument("--model", default=None)
    sync.add_argument("--max-llm-requests", type=int, default=5)


def _read_local(path: str, max_bytes: int) -> bytes:
    target = Path(path)
    if not target.is_file():
        raise AppError("Файл не найден или это не обычный файл")
    if target.stat().st_size > max_bytes:
        raise AppError("Файл больше лимита RECEIPT_MAX_BYTES")
    with target.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AppError("Файл больше лимита RECEIPT_MAX_BYTES")
    return data


def _safe(value: Any) -> str:
    return sanitize_for_terminal(str(value)) if value is not None else "—"


def _print_receipt(row: dict[str, Any]) -> None:
    print(
        f"  #{row['id']}: {_safe(row['purchase_date'])} {_safe(row['purchase_time'])} | "
        f"{_safe(row['merchant_name'])} | {format_amount(row['amount_minor'], row['currency'])} | "
        f"{row['operation_type']} | {row['category']} | статус: {row['review_status']}"
    )
    for warning in _warnings(row):
        print(f"      ! {_safe(warning)}")


def _warnings(row: dict[str, Any]) -> list[str]:
    return json.loads(row["warnings_json"]) if row.get("warnings_json") else []


def _build_ai(allow_cloud: bool, model: str | None, budget: int) -> AiOptions:
    if not allow_cloud:
        return AiOptions(allow_cloud=False, llm_settings=None, budget=LLMBudget(0))
    return AiOptions(allow_cloud=True, llm_settings=load_llm_settings(model), budget=LLMBudget(budget))


def cmd_receipt_extract(args: argparse.Namespace) -> int:
    settings = load_receipt_settings()
    data = _read_local(args.path, settings.max_bytes)
    ocr = TesseractOcr(settings.ocr_languages, settings.ocr_timeout_seconds)
    ai = _build_ai(args.allow_cloud, args.model, 1)
    name = sanitize_for_terminal(os.path.basename(args.path))[:_MAX_LOCAL_NAME]
    with Store(settings.db_path) as store:
        if args.reprocess:
            print("Внимание: --reprocess выполнит новый платный AI-запрос, если указан --allow-cloud.")
        result = process_document(
            store, data, source=SourceRef(part_name=name), settings=settings, ocr=ocr, ai=ai,
            reprocess=args.reprocess,
        )
        print(f"Документ #{result.document_id}, SHA-256 {result.sha256[:12]}…")
        print(f"Статус: {result.status}" + (f", ошибка: {result.error_code}" if result.error_code else ""))
        if result.error_code in ERROR_HINTS:
            print(f"Причина: {ERROR_HINTS[result.error_code]}")
        if result.duplicate:
            print("Документ уже был в базе: повторная обработка выполнена только в объёме, описанном ниже.")
        for note in result.notes:
            print(f"Примечание: {note}")
        if not result.text_complete and result.status != "failed":
            print("Текст неполный: результат помечен для проверки.")
        rows = [dict(row) for row in store.current_receipts(result.document_id)]
        if rows:
            print(f"Чеков в документе: {len(rows)} (статус извлечения; расход создаётся только после confirm)")
            for row in rows:
                _print_receipt(row)
        if args.show_text:
            document = store.document(result.document_id)
            print("--- текст документа (отладка) ---")
            print(sanitize_for_terminal(document["extracted_text"] or "", multiline=True))
    return 1 if result.error_code or result.status == "failed" else 0


def cmd_receipt_list(args: argparse.Namespace) -> int:
    settings = load_receipt_settings()
    with Store(settings.db_path) as store:
        rows = [
            dict(row)
            for row in store.conn.execute(
                "SELECT * FROM receipts WHERE is_current = 1 ORDER BY purchase_date IS NULL, purchase_date, id LIMIT ?",
                (args.limit,),
            ).fetchall()
        ]
    if not rows:
        print("Чеков нет. Добавьте документ: receipt extract ./файл.pdf")
        return 0
    for row in rows:
        _print_receipt(row)
    return 0


def cmd_receipt_show(args: argparse.Namespace) -> int:
    settings = load_receipt_settings()
    with Store(settings.db_path) as store:
        row = receipt_view(store, args.id)
        document = store.document(row["document_id"])
        _print_receipt(row)
        print(f"  Документ #{row['document_id']}, SHA-256 {document['sha256'][:12]}…, текст полный: "
              f"{'да' if document['text_complete'] else 'нет'}")
        print(f"  Тип: {document['mime_type']}, страниц: {document['page_count'] or '—'}")
        print(f"  Источники: {len(store.sources_for(row['document_id']))}")
        for label, key in (("Магазин", "merchant"), ("Дата", "date"), ("Сумма", "amount"), ("Операция", "operation")):
            quote = _evidence(row).get(key)
            if quote:
                print(f"  Цитата ({label}): {_safe(quote)}")
        if row["receipt_number"]:
            print(f"  Номер чека: {_safe(row['receipt_number'])}")
        if row["fiscal_id"]:
            print(f"  Фискальный ID: {_safe(row['fiscal_id'])}")
        for correction in store.corrections_for(args.id):
            print(f"  Исправлено: {correction['field']}: {_safe(correction['old_value'])} → {_safe(correction['new_value'])}")
        if args.text:
            print("--- текст документа (отладка) ---")
            print(sanitize_for_terminal(document["extracted_text"] or "", multiline=True))
    return 0


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["evidence_json"]) if row.get("evidence_json") else {}


def cmd_receipt_review() -> int:
    settings = load_receipt_settings()
    with Store(settings.db_path) as store:
        queue = review_queue(store)
        suspects = duplicate_suspects(store)
    print(f"На проверку: {len(queue)}")
    for row in queue:
        _print_receipt(row)
    print(f"Подозрения на дубль (не удаляются автоматически): {len(suspects)}")
    for group in suspects:
        print(
            f"  {_safe(group['purchase_date'])} | {_safe(group['merchant_name'])} | "
            f"{format_amount(group['amount_minor'], group['currency'])} | документов: {group['documents']} | "
            f"чеки: {group['receipt_ids']}"
        )
    return 0


def cmd_receipt_backup(args: argparse.Namespace) -> int:
    import hashlib
    import sqlite3
    from datetime import UTC, datetime

    settings = load_receipt_settings()
    if not settings.db_path.exists():
        raise AppError("База ещё не создана: резервная копия не нужна")
    target = Path(args.out) if args.out else settings.db_path.parent / "backups" / (
        f"receipts-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    )
    if target.exists():
        raise AppError("Файл резервной копии уже существует")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    source = sqlite3.connect(settings.db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(target, 0o600)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"Резервная копия: {target}")
    print(f"SHA-256: {digest}")
    return 0


def cmd_receipt_edit(args: argparse.Namespace) -> int:
    changes = {key: getattr(args, key) for key in EDITABLE_FIELDS if getattr(args, key) is not None}
    if not changes:
        raise ReceiptError("Укажите хотя бы одно поле: --merchant, --date, --time, --amount, --currency, --category или --operation")
    settings = load_receipt_settings()
    with Store(settings.db_path) as store:
        history = edit_receipt(store, args.id, changes)
    if not history:
        print("Изменений нет: значения совпадают с текущими.")
    for field_name, old, new in history:
        print(f"Исправлено {field_name}: {_safe(old)} → {_safe(new)}")
    return 0


def cmd_receipt_confirm(args: argparse.Namespace) -> int:
    settings = load_receipt_settings()
    with Store(settings.db_path) as store:
        partial = confirm_receipt(store, args.id)
    print(f"Чек #{args.id} подтверждён и включён в расходы.")
    if partial:
        print("Внимание: текст документа неполный (OCR или обрезка). Отметка сохранена в записи.")
    return 0


def cmd_receipt_reject(args: argparse.Namespace) -> int:
    settings = load_receipt_settings()
    with Store(settings.db_path) as store:
        reject_receipt(store, args.id)
    print(f"Чек #{args.id} отклонён и не входит в отчёты.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = load_receipt_settings()
    parse_period(args.date_from.isoformat(), args.date_to.isoformat())
    with Store(settings.db_path) as store:
        report = build_report(store, args.date_from.isoformat(), args.date_to.isoformat())
    for line in render_report(report):
        print(line)
    if args.csv:
        count = write_csv(report, Path(args.csv), force=args.force)
        print(f"CSV сохранён: {args.csv} (строк: {count})")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    validate_period(args.received_from, args.received_to)
    validate_batch(args.max_messages)
    mail = load_mail_settings()
    receipts = load_receipt_settings()
    if args.dry_run:
        with ImapSession(mail, args.mailbox) as session:
            plan = plan_sync(session, args.received_from, args.received_to, args.max_messages, args.kinds, args.min_bytes)
        print("Dry-run: вложения не загружались, LLM не вызывался, почта не изменена.")
        for line in describe_plan(plan, args.max_messages):
            print(line)
        return 0
    if not args.allow_cloud and not args.local_only:
        raise SyncError("Реальный запуск sync требует --allow-cloud или --local-only (dry-run работает без них)")
    if args.allow_cloud and args.local_only:
        raise SyncError("Укажите только один режим: --allow-cloud или --local-only")
    if not 1 <= args.max_documents <= 20:
        raise SyncError("--max-documents должен быть от 1 до 20")
    if args.max_llm_requests < 0 or args.max_llm_requests > 50:
        raise SyncError("--max-llm-requests должен быть от 0 до 50")
    if args.allow_cloud:
        llm = load_llm_settings(args.model)
        if not llm.email_use_confirmed:
            raise AppError("Облачная обработка заблокирована: LLM_EMAIL_USE_CONFIRMED не равен true")
        ai = AiOptions(allow_cloud=True, llm_settings=llm, budget=LLMBudget(args.max_llm_requests))
    else:
        ai = AiOptions(allow_cloud=False, llm_settings=None, budget=LLMBudget(0))
    ocr = TesseractOcr(receipts.ocr_languages, receipts.ocr_timeout_seconds)
    with Store(receipts.db_path) as store, ImapSession(mail, args.mailbox) as session:
        plan = plan_sync(session, args.received_from, args.received_to, args.max_messages, args.kinds, args.min_bytes)
        for line in describe_plan(plan, args.max_messages):
            print(line)
        stats = run_sync(session, store, mail.username, plan, receipts, ocr, ai, max_documents=args.max_documents)
    for line in describe_stats(stats):
        print(line)
    return 0


def dispatch_receipt(args: argparse.Namespace) -> int:
    if args.command == "receipt":
        handlers = {
            "extract": lambda: cmd_receipt_extract(args),
            "list": lambda: cmd_receipt_list(args),
            "show": lambda: cmd_receipt_show(args),
            "review": cmd_receipt_review,
            "backup": lambda: cmd_receipt_backup(args),
            "edit": lambda: cmd_receipt_edit(args),
            "confirm": lambda: cmd_receipt_confirm(args),
            "reject": lambda: cmd_receipt_reject(args),
        }
        return handlers[args.receipt_command]()
    if args.command == "report":
        return cmd_report(args)
    return cmd_sync(args)
