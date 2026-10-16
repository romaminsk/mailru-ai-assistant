from __future__ import annotations

import codecs
import email.policy
import re
import unicodedata
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser

from mail_assistant.config import AppError

ATTACHMENT_NAME_LIMIT = 120
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_KEEP_FORMAT = frozenset({"\u200d"})
_DROP_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Cn", "Zl", "Zp"})
_SKIP_TAGS = frozenset({"script", "style"})
_BLOCK_TAGS = frozenset(
    {"br", "p", "div", "li", "tr", "table", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_QUOTES = str.maketrans({
    "«": '"', "»": '"', "“": '"', "”": '"', "„": '"',
    "‘": "'", "’": "'", "‚": "'", "‹": "'", "›": "'",
})
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})


class EmailParseError(AppError):
    """Письмо не удалось разобрать."""


@dataclass(frozen=True)
class ParsedEmail:
    sender: str
    subject: str
    date: str
    body_text: str
    body_kind: str
    attachments: tuple[str, ...]


def _repair_text(text: str) -> str:
    """Восстанавливает байты, которые email-парсер сохранил через surrogateescape."""
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def sanitize_for_terminal(text: str, *, multiline: bool = False) -> str:
    """Убирает управляющие последовательности и скрытые символы перед выводом."""
    text = _ANSI_RE.sub("", _repair_text(text))
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append("\n" if multiline else " ")
        elif ch in "\t\r\v\f":
            out.append(" ")
        else:
            category = unicodedata.category(ch)
            if category in _DROP_CATEGORIES:
                continue
            if category == "Cf" and ch not in _KEEP_FORMAT:
                continue
            out.append(ch)
    result = "".join(out)
    if multiline:
        lines = [line.rstrip() for line in result.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return " ".join(result.split())


def normalize_for_match(text: str) -> str:
    """Нормализация для сверки evidence: NFKC, кавычки и тире, регистр, пробелы."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).translate(_DASHES)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return " ".join(text.casefold().split())


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Текст из HTML без выполнения кода, без script/style и без внешних запросов."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        pass
    lines = [" ".join(line.split()) for line in extractor.text().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _header(msg: Message, name: str) -> str:
    """Сырое значение заголовка: восстановление байтов, затем RFC 2047."""
    try:
        raw_values = [value for key, value in msg.raw_items() if key.lower() == name]
        if not raw_values:
            return ""
        text = _repair_text(str(raw_values[0]))
        try:
            text = str(make_header(decode_header(text)))
        except Exception:
            pass
        return sanitize_for_terminal(text)
    except Exception:
        return ""


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    return payload.decode(charset, errors="replace")


def _body_and_attachments(msg: Message) -> tuple[str, str, list[str]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if part.get_content_disposition() == "attachment" or filename:
            name = sanitize_for_terminal(filename or "без имени")
            attachments.append(name[:ATTACHMENT_NAME_LIMIT])
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_part_text(part))
        elif content_type == "text/html":
            html.append(_part_text(part))

    plain_text = "\n".join(p for p in plain if p.strip())
    if plain_text.strip():
        return sanitize_for_terminal(plain_text, multiline=True), "plain", attachments
    html_text = html_to_text("\n".join(html))
    if html_text:
        return sanitize_for_terminal(html_text, multiline=True), "html", attachments
    return "", "none", attachments


def parse_email(raw: bytes) -> ParsedEmail:
    """Разбор письма без сохранения и без исполнения вложений."""
    try:
        msg = BytesParser(policy=email.policy.default).parsebytes(raw)
        body, kind, attachments = _body_and_attachments(msg)
    except Exception:
        raise EmailParseError("Не удалось разобрать письмо") from None
    return ParsedEmail(
        sender=_header(msg, "from"),
        subject=_header(msg, "subject") or "(без темы)",
        date=_header(msg, "date"),
        body_text=body,
        body_kind=kind,
        attachments=tuple(attachments),
    )
