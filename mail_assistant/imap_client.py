from __future__ import annotations

import base64
import imaplib
import re
import ssl
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from mail_assistant.config import AppError, MailSettings
from mail_assistant.parser import ParsedEmail, parse_email

MAILBOX_DEFAULT = "INBOX"
HEADER_ITEMS = "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
MAX_UID = 2**32 - 1
MAILBOX_MAX_LEN = 200
_SEQ_UID_RE = re.compile(rb"^\s*(\d+)\s+\(.*?\bUID\s+(\d+)")
_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)")
_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S.*)$'
)
_UNSAFE_FLAGS = frozenset({"\\noselect", "\\nonexistent"})
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailError(AppError):
    """Ошибка работы с почтой; текст безопасен для вывода."""


@dataclass(frozen=True)
class MailboxInfo:
    mailbox_id: str
    display_name: str
    flags: frozenset[str]

    @property
    def selectable(self) -> bool:
        return not (self.flags & _UNSAFE_FLAGS)


def validate_uid(uid: int) -> int:
    if isinstance(uid, bool) or not isinstance(uid, int) or not 1 <= uid <= MAX_UID:
        raise MailError("UID должен быть положительным целым числом")
    return uid


def validate_mailbox_id(value: str) -> str:
    """Идентификатор папки: ASCII (modified UTF-7), без CR/LF и управляющих символов."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAILBOX_MAX_LEN:
        raise MailError(f"Имя папки должно быть от 1 до {MAILBOX_MAX_LEN} символов")
    if value != value.strip():
        raise MailError("Имя папки не должно начинаться или заканчиваться пробелом")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise MailError("Имя папки содержит управляющие символы (включая CR/LF)")
    if not value.isascii():
        raise MailError("Укажите идентификатор из 'mail folders' (ASCII, кодировка modified UTF-7)")
    return value


def imap_quote(name: str) -> str:
    """Строка IMAP в кавычках: экранируются обратная косая черта и кавычка."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def decode_modified_utf7(text: str) -> str:
    """Декодирование IMAP modified UTF-7 (RFC 3501). При ошибке возвращается исходная строка."""
    out: list[str] = []
    index = 0
    try:
        while index < len(text):
            ch = text[index]
            if ch != "&":
                out.append(ch)
                index += 1
                continue
            end = text.find("-", index)
            if end == -1:
                return text
            chunk = text[index + 1 : end]
            if chunk == "":
                out.append("&")
            else:
                padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
                raw = base64.b64decode(padded, validate=True)
                if len(raw) % 2:
                    return text
                out.append(raw.decode("utf-16-be"))
            index = end + 1
    except ValueError:
        return text
    return "".join(out)


def _quote_bytes(value: bytes) -> bytes:
    return b'"' + re.sub(rb'(["\\])', rb"\\\1", value) + b'"'


def _iter_list_lines(data: list[Any]) -> Iterator[bytes]:
    for item in data:
        if isinstance(item, bytes):
            yield item
        elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], bytes) and isinstance(item[1], bytes):
            meta, literal = item
            brace = meta.rfind(b"{")
            if brace >= 0:
                yield meta[:brace].rstrip() + b" " + _quote_bytes(literal)


def parse_list_response(data: list[Any]) -> list[MailboxInfo]:
    """Разбор ответа LIST. Строки, которые не удалось разобрать, пропускаются."""
    result: list[MailboxInfo] = []
    for line in _iter_list_lines(data):
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        flags = frozenset(part.lower() for part in match.group("flags").decode("ascii", "replace").split())
        raw_name = match.group("name")
        if raw_name.startswith(b'"'):
            raw_name = re.sub(rb"\\(.)", rb"\1", raw_name[1:-1])
        mailbox_id = raw_name.decode("utf-8", "replace")
        result.append(
            MailboxInfo(
                mailbox_id=mailbox_id,
                display_name=decode_modified_utf7(mailbox_id),
                flags=flags,
            )
        )
    return result


def _iter_fetch_items(data: list[Any]) -> Iterator[tuple[bytes, bytes | None]]:
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], bytes):
            literal = item[1] if isinstance(item[1], bytes) else None
            yield item[0], literal
        elif isinstance(item, bytes):
            yield item, None


def _uid_of(meta: bytes) -> int | None:
    match = _UID_RE.search(meta)
    return int(match.group(1)) if match else None


def _logout_quietly(conn: imaplib.IMAP4_SSL) -> None:
    """Ошибки очистки не должны скрывать исходную ошибку."""
    try:
        conn.logout()
    except Exception:
        with suppress(Exception):
            conn.shutdown()


def _uidvalidity(conn: imaplib.IMAP4_SSL) -> int | None:
    try:
        _, data = conn.response("UIDVALIDITY")
    except Exception:
        return None
    if data and isinstance(data[0], bytes) and data[0].isdigit():
        return int(data[0])
    return None


def imap_date(value: date) -> str:
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


def _find_mailbox(infos: list[MailboxInfo], wanted: str) -> MailboxInfo | None:
    if wanted.upper() == MAILBOX_DEFAULT:
        return next((info for info in infos if info.mailbox_id.upper() == MAILBOX_DEFAULT), None)
    return next((info for info in infos if info.mailbox_id == wanted), None)


class ImapSession:
    """Сессия IMAP только для чтения: без STORE, MOVE, EXPUNGE, APPEND и SMTP.

    Папка открывается через SELECT с readonly=True. UID действителен только внутри выбранной папки.
    """

    def __init__(self, settings: MailSettings, mailbox: str = MAILBOX_DEFAULT) -> None:
        self._settings = settings
        self._mailbox_id = validate_mailbox_id(mailbox)
        self._conn: imaplib.IMAP4_SSL | None = None
        self._opened: tuple[MailboxInfo, int] | None = None
        self.uidvalidity: int | None = None

    def __enter__(self) -> ImapSession:
        self._conn = self._connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        conn, self._conn = self._conn, None
        self._opened = None
        self.uidvalidity = None
        if conn is not None:
            _logout_quietly(conn)

    def _connect(self) -> imaplib.IMAP4_SSL:
        self._settings.require_credentials()
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            conn = imaplib.IMAP4_SSL(
                self._settings.host,
                self._settings.port,
                ssl_context=context,
                timeout=self._settings.timeout_seconds,
            )
        except (OSError, imaplib.IMAP4.error):
            raise MailError("Не удалось установить TLS-соединение с IMAP-сервером") from None
        try:
            conn.login(self._settings.username, self._settings.app_password)
        except (OSError, imaplib.IMAP4.error):
            _logout_quietly(conn)
            raise MailError("Вход не выполнен: проверьте MAIL_USERNAME и пароль внешнего приложения") from None
        return conn

    def _conn_or_fail(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise MailError("IMAP-сессия не открыта")
        return self._conn

    @staticmethod
    def _call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> list[Any]:
        try:
            status, data = func(*args, **kwargs)
        except (OSError, imaplib.IMAP4.error):
            raise MailError("Ошибка IMAP-команды или таймаут сети") from None
        if status != "OK":
            raise MailError("IMAP-сервер отклонил команду")
        return data or []

    def list_mailboxes(self) -> list[MailboxInfo]:
        data = self._call(self._conn_or_fail().list)
        return parse_list_response(data)

    def open_mailbox(self) -> tuple[MailboxInfo, int]:
        """Проверяет папку по LIST, отклоняет \\Noselect и открывает её через SELECT readonly=True."""
        if self._opened is not None:
            return self._opened
        infos = self.list_mailboxes()
        info = _find_mailbox(infos, self._mailbox_id)
        if info is None:
            raise MailError(f"Папка {self._mailbox_id} не найдена на сервере (см. mail folders)")
        if not info.selectable:
            raise MailError(f"Папка {info.display_name} недоступна для выбора (\\Noselect)")
        data = self._call(self._conn_or_fail().select, imap_quote(info.mailbox_id), readonly=True)
        raw = data[0] if data else None
        if not isinstance(raw, bytes) or not raw.isdigit():
            raise MailError("Некорректный ответ сервера на SELECT")
        self.uidvalidity = _uidvalidity(self._conn_or_fail())
        self._opened = (info, int(raw))
        return self._opened

    def recent_uids(self, limit: int) -> list[int]:
        """UID последних писем по порядковым номерам (порядок папки, не сортировка по дате)."""
        _, count = self.open_mailbox()
        if count == 0:
            return []
        first = max(1, count - limit + 1)
        data = self._call(self._conn_or_fail().fetch, f"{first}:{count}", "(UID)")
        pairs: list[tuple[int, int]] = []
        for meta, _ in _iter_fetch_items(data):
            match = _SEQ_UID_RE.match(meta)
            if match:
                pairs.append((int(match.group(1)), int(match.group(2))))
        pairs.sort()
        return [uid for _, uid in pairs][-limit:]

    def recent_headers(self, limit: int) -> list[tuple[int, ParsedEmail | None]]:
        uids = self.recent_uids(limit)
        if not uids:
            return []
        data = self._call(
            self._conn_or_fail().uid, "FETCH", ",".join(str(uid) for uid in uids), HEADER_ITEMS
        )
        literals: dict[int, bytes] = {}
        for meta, literal in _iter_fetch_items(data):
            uid = _uid_of(meta)
            if uid is not None and literal is not None:
                literals[uid] = literal
        return [
            (uid, parse_email(literals[uid]) if uid in literals else None)
            for uid in uids
        ]

    def search_received(self, start: date, end: date) -> list[int]:
        """UID писем, полученных с start по end включительно (IMAP SINCE/BEFORE, без SEARCH ALL)."""
        self.open_mailbox()
        data = self._call(
            self._conn_or_fail().uid, "SEARCH", "SINCE", imap_date(start), "BEFORE", imap_date(end + timedelta(days=1))
        )
        raw = data[0] if data and isinstance(data[0], bytes) else b""
        return sorted(int(part) for part in raw.split() if part.isdigit())

    def fetch_sizes(self, uids: list[int]) -> dict[int, int]:
        """Размеры писем (RFC822.SIZE) без загрузки тел."""
        self.open_mailbox()
        if not uids:
            return {}
        data = self._call(self._conn_or_fail().uid, "FETCH", ",".join(str(uid) for uid in uids), "(RFC822.SIZE)")
        sizes: dict[int, int] = {}
        for meta, _ in _iter_fetch_items(data):
            uid = _uid_of(meta)
            match = _SIZE_RE.search(meta)
            if uid is not None and match:
                sizes[uid] = int(match.group(1))
        return sizes

    def fetch_structures(self, uids: list[int]) -> dict[int, bytes]:
        """Строки BODYSTRUCTURE для отбора писем с вложениями; тела писем не загружаются."""
        self.open_mailbox()
        if not uids:
            return {}
        data = self._call(self._conn_or_fail().uid, "FETCH", ",".join(str(uid) for uid in uids), "(UID BODYSTRUCTURE)")
        structures: dict[int, bytes] = {}
        for meta, _ in _iter_fetch_items(data):
            uid = _uid_of(meta)
            if uid is not None and b"BODYSTRUCTURE" in meta.upper():
                structures[uid] = meta
        return structures

    def fetch_message(self, uid: int) -> bytes:
        """Загружает письмо через BODY.PEEK[] после проверки размера по RFC822.SIZE."""
        validate_uid(uid)
        self.open_mailbox()
        conn = self._conn_or_fail()
        limit = self._settings.max_message_bytes
        size = self._fetch_size(conn, uid)
        if size > limit:
            raise MailError(f"Письмо слишком большое: {size} байт при лимите {limit}. Загрузка отменена.")
        data = self._call(conn.uid, "FETCH", str(uid), "(BODY.PEEK[])")
        raw: bytes | None = None
        for meta, literal in _iter_fetch_items(data):
            if literal is not None and _uid_of(meta) == uid:
                raw = literal
                break
        if raw is None:
            raise MailError("Письмо не найдено или недоступно в выбранной папке")
        if len(raw) > limit:
            raise MailError("Загруженное письмо превышает лимит размера")
        return raw

    def _fetch_size(self, conn: imaplib.IMAP4_SSL, uid: int) -> int:
        data = self._call(conn.uid, "FETCH", str(uid), "(RFC822.SIZE)")
        for meta, _ in _iter_fetch_items(data):
            if _uid_of(meta) == uid:
                match = _SIZE_RE.search(meta)
                if match:
                    return int(match.group(1))
        raise MailError("Письмо с таким UID не найдено в выбранной папке")
