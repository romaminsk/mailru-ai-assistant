from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence

from mail_assistant.analysis import build_payload, parse_analysis, sanitize_tree, verify_evidence
from mail_assistant.config import AppError, load_env_file, load_llm_settings, load_mail_settings
from mail_assistant.imap_client import MAILBOX_DEFAULT, MAX_UID, ImapSession, MailboxInfo, MailError, validate_mailbox_id
from mail_assistant.llm_client import LLMClient
from mail_assistant.parser import parse_email, sanitize_for_terminal, truncate_text
from mail_assistant.receipt_cli import add_receipt_parsers, dispatch_receipt


def _uid_arg(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > MAX_UID:
        raise argparse.ArgumentTypeError("UID должен быть положительным целым числом")
    return int(value)


def _mailbox_arg(value: str) -> str:
    try:
        return validate_mailbox_id(value)
    except MailError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _folder_line(info: MailboxInfo) -> str:
    return f"{sanitize_for_terminal(info.display_name)} [id: {sanitize_for_terminal(info.mailbox_id)}]"


def _int_in_range(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        if not re.fullmatch(r"[0-9]{1,6}", value):
            raise argparse.ArgumentTypeError(f"нужно целое число от {low} до {high}")
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"значение должно быть от {low} до {high}")
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mail_assistant",
        description="Чтение почты Mail.ru по IMAP и AI-анализ выбранного письма.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    mail = commands.add_parser("mail", help="работа с почтой (только чтение)")
    mail_commands = mail.add_subparsers(dest="mail_command", required=True)
    mail_commands.add_parser("folders", help="список папок IMAP (LIST) и идентификаторы для --mailbox")
    check_cmd = mail_commands.add_parser("check", help="проверить подключение и количество писем")
    check_cmd.add_argument("--mailbox", type=_mailbox_arg, default=MAILBOX_DEFAULT)
    list_cmd = mail_commands.add_parser("list", help="последние письма: UID, дата, отправитель, тема")
    list_cmd.add_argument("--limit", type=_int_in_range(1, 20), default=5)
    list_cmd.add_argument("--mailbox", type=_mailbox_arg, default=MAILBOX_DEFAULT)
    read_cmd = mail_commands.add_parser("read", help="прочитать письмо по UID")
    read_cmd.add_argument("--uid", type=_uid_arg, required=True)
    read_cmd.add_argument("--max-chars", type=_int_in_range(1, 20000), default=2000)
    read_cmd.add_argument("--mailbox", type=_mailbox_arg, default=MAILBOX_DEFAULT)

    llm = commands.add_parser("llm", help="проверка LLM API")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    llm_commands.add_parser("models", help="показать ID моделей из каталога")
    check_cmd = llm_commands.add_parser("check", help="короткий синтетический smoke test")
    check_cmd.add_argument("--model", default=None)

    analyze = commands.add_parser("analyze", help="AI-анализ одного письма (отправляется наружу)")
    analyze.add_argument("--uid", type=_uid_arg, required=True)
    analyze.add_argument("--model", default=None)
    analyze.add_argument("--allow-cloud", action="store_true")
    analyze.add_argument("--mailbox", type=_mailbox_arg, default=MAILBOX_DEFAULT)
    add_receipt_parsers(commands)
    return parser


def cmd_mail_folders() -> int:
    with ImapSession(load_mail_settings()) as session:
        infos = session.list_mailboxes()
    if not infos:
        print("Сервер не вернул папок.")
        return 0
    print(f"Папки IMAP (LIST): {len(infos)}")
    print("Для --mailbox используйте идентификатор в скобках [id: ...]. Веб-группы без строки здесь не являются папками.")
    for info in infos:
        status = "выбираемая" if info.selectable else "недоступна для выбора (\\Noselect)"
        print(f"  {_folder_line(info)} — {status}")
    return 0


def cmd_mail_check(mailbox: str) -> int:
    settings = load_mail_settings()
    with ImapSession(settings, mailbox) as session:
        info, count = session.open_mailbox()
    print("IMAP: подключение и вход выполнены.")
    print(f"Папка открыта только для чтения: {_folder_line(info)}. Писем: {count}.")
    return 0


def cmd_mail_list(limit: int, mailbox: str) -> int:
    settings = load_mail_settings()
    with ImapSession(settings, mailbox) as session:
        info, _ = session.open_mailbox()
        rows = session.recent_headers(limit)
    if not rows:
        print(f"Папка {_folder_line(info)} пуста.")
        return 0
    print(f"Папка: {_folder_line(info)}")
    print(f"Последние сообщения: {len(rows)} (порядок папки, не сортировка по дате отправки).")
    for uid, parsed in rows:
        if parsed is None:
            print(f"UID {uid}: сообщение недоступно (возможно, удалено).")
            continue
        print(f"UID {uid}")
        print(f"  Дата:       {parsed.date or '—'}")
        print(f"  От:         {parsed.sender or '—'}")
        print(f"  Тема:       {parsed.subject}")
    return 0


def cmd_mail_read(uid: int, max_chars: int, mailbox: str) -> int:
    settings = load_mail_settings()
    with ImapSession(settings, mailbox) as session:
        info, _ = session.open_mailbox()
        raw = session.fetch_message(uid)
    parsed = parse_email(raw)
    body, truncated = truncate_text(parsed.body_text, max_chars)
    print(f"Папка: {_folder_line(info)}")
    print(f"UID:   {uid}")
    print(f"Дата:  {parsed.date or '—'}")
    print(f"От:    {parsed.sender or '—'}")
    print(f"Тема:  {parsed.subject}")
    if parsed.attachments:
        names = ", ".join(parsed.attachments)
        print(f"Вложения: {len(parsed.attachments)} (не открыты, не исполнены, не сохранены): {names}")
    else:
        print("Вложения: нет")
    if parsed.body_kind == "html":
        print("Примечание: текст извлечён из HTML (без скриптов и внешних запросов).")
    print("-" * 40)
    print(body if body else "(текстовое содержимое не найдено)")
    if truncated:
        print(f"\n[Текст усечён до {max_chars} символов]")
    return 0


def cmd_llm_models() -> int:
    settings = load_llm_settings()
    with LLMClient(settings) as client:
        ids = client.list_models()
    if not ids:
        print("Каталог моделей пуст.")
    else:
        print(f"Модели из каталога ({len(ids)}):")
        for model_id in ids:
            print(f"  {sanitize_for_terminal(model_id)}")
    print("Внимание: наличие модели в каталоге не означает совместимость с Chat Completions endpoint этого проекта.")
    print("Поддерживаются только модели с Chat Completions; Responses и Messages API не поддерживаются.")
    return 0


def cmd_llm_check(model: str | None) -> int:
    settings = load_llm_settings(model)
    messages = [
        {"role": "system", "content": "Ты помощник по программированию. Отвечай кратко и по делу."},
        {"role": "user", "content": "Объясни в двух-трёх предложениях, что делает встроенная функция Python enumerate()."},
    ]
    with LLMClient(settings) as client:
        result = client.chat(messages, settings.model)
    print("Smoke test API: ответ получен.")
    print("Это проверка доступности API, а не подтверждение разрешения использовать подписку для анализа почты.")
    print(f"Запрошенная модель: {settings.model}")
    print(f"Модель по данным ответа: {result.reported_model or 'не указана'}")
    if result.usage:
        print("Usage: " + ", ".join(f"{key}={value}" for key, value in result.usage.items()))
    print()
    print(sanitize_for_terminal(result.content, multiline=True))
    return 0


def cmd_analyze(uid: int, model: str | None, allow_cloud: bool, mailbox: str) -> int:
    settings = load_llm_settings(model)
    if not allow_cloud:
        raise AppError("Письмо не будет отправлено наружу без флага --allow-cloud")
    if not settings.email_use_confirmed:
        raise AppError(
            "Анализ писем заблокирован: допустимость провайдера не подтверждена "
            "(LLM_EMAIL_USE_CONFIRMED=false)"
        )
    with ImapSession(load_mail_settings(), mailbox) as session:
        info, _ = session.open_mailbox()
        raw = session.fetch_message(uid)
    email_data = parse_email(raw)
    payload = build_payload(email_data, settings.max_input_chars)
    with LLMClient(settings) as client:
        result = client.chat(payload.messages, settings.model)
    analysis = parse_analysis(result.content, result.diagnostics)
    verified = verify_evidence(analysis, payload.source_text)
    report = {
        "mailbox": {"id": info.mailbox_id, "display_name": info.display_name},
        "uid": uid,
        "subject": email_data.subject,
        "input_truncated": payload.truncated,
        "model_requested": settings.model,
        "model_reported": result.reported_model,
        "usage": result.usage,
        "dropped_unverified_tasks": verified.dropped_tasks,
        "analysis": verified.analysis.model_dump(),
    }
    print(json.dumps(sanitize_tree(report), ensure_ascii=False, indent=2))
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command in ("receipt", "report", "sync"):
        return dispatch_receipt(args)
    if args.command == "mail":
        if args.mail_command == "folders":
            return cmd_mail_folders()
        if args.mail_command == "check":
            return cmd_mail_check(args.mailbox)
        if args.mail_command == "list":
            return cmd_mail_list(args.limit, args.mailbox)
        return cmd_mail_read(args.uid, args.max_chars, args.mailbox)
    if args.command == "llm":
        if args.llm_command == "models":
            return cmd_llm_models()
        return cmd_llm_check(args.model)
    return cmd_analyze(args.uid, args.model, args.allow_cloud, args.mailbox)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file()
    try:
        return _dispatch(args)
    except AppError as exc:
        print(f"Ошибка: {sanitize_for_terminal(str(exc))}", file=sys.stderr)
        if exc.details:
            print(
                "Метаданные ответа (без текстов): " + json.dumps(sanitize_tree(exc.details), ensure_ascii=False),
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Внутренняя ошибка ({type(exc).__name__}). Детали скрыты, чтобы не раскрыть данные.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
