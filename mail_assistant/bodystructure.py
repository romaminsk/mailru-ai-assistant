"""Разбор IMAP BODYSTRUCTURE без загрузки тел писем.

Используется для dry-run и отбора писем с PNG/PDF-вложениями: тело письма скачивается
только у писем, где структура содержит кандидатов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mail_assistant.parser import sanitize_for_terminal

CANDIDATE_TYPES = frozenset({"application/pdf", "image/png"})
CANDIDATE_SUFFIXES = (".pdf", ".png")
MAX_DEPTH = 20


class StructureError(ValueError):
    """Структура не разобрана; вызывающий код относится к письму осторожно."""


@dataclass(frozen=True)
class Attachment:
    mime_type: str
    name: str
    size: int

    @property
    def is_candidate(self) -> bool:
        return self.mime_type in CANDIDATE_TYPES or self.name.lower().endswith(CANDIDATE_SUFFIXES)


_TOKEN_RE = re.compile(rb'\s*(\(|\)|"(?:[^"\\]|\\.)*"|\{\d+\}|NIL|[^\s()"]+)', re.IGNORECASE)


def _tokenize(raw: bytes) -> list[bytes]:
    tokens = []
    position = 0
    while position < len(raw):
        if raw[position:position + 1].isspace():
            position += 1
            continue
        match = _TOKEN_RE.match(raw, position)
        if not match:
            raise StructureError("неожиданный символ в BODYSTRUCTURE")
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def _parse_tokens(tokens: list[bytes], start: int, depth: int) -> tuple[Any, int]:
    if depth > MAX_DEPTH:
        raise StructureError("слишком глубокая структура")
    if start >= len(tokens) or tokens[start] != b"(":
        raise StructureError("ожидалась открывающая скобка")
    items: list[Any] = []
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token == b")":
            return items, index + 1
        if token == b"(":
            value, index = _parse_tokens(tokens, index, depth + 1)
            items.append(value)
            continue
        if token.upper() == b"NIL":
            items.append(None)
        elif token.startswith(b'"') and token.endswith(b'"') and len(token) >= 2:
            items.append(re.sub(rb"\\(.)", rb"\1", token[1:-1]).decode("utf-8", "replace"))
        elif token.isdigit():
            items.append(int(token))
        else:
            items.append(token.decode("utf-8", "replace"))
        index += 1
    raise StructureError("незакрытая скобка в BODYSTRUCTURE")


def parse_fetch_structure(meta: bytes) -> Any:
    """Разбирает выражение BODYSTRUCTURE из строки ответа FETCH (лишние закрывающие скобки игнорируются)."""
    marker = meta.upper().find(b"BODYSTRUCTURE")
    if marker < 0:
        raise StructureError("в ответе нет BODYSTRUCTURE")
    tokens = _tokenize(meta[marker + len(b"BODYSTRUCTURE"):])
    tree, _ = _parse_tokens(tokens, 0, 0)
    if not tree:
        raise StructureError("пустая BODYSTRUCTURE")
    return tree


def _param_value(params: Any, key: str) -> str | None:
    if not isinstance(params, list):
        return None
    for index in range(0, len(params) - 1, 2):
        name = params[index]
        if isinstance(name, str) and name.lower() == key:
            value = params[index + 1]
            return value if isinstance(value, str) else None
    return None


def _single_part(part: list[Any]) -> Attachment:
    if len(part) < 7 or not isinstance(part[0], str) or not isinstance(part[1], str):
        raise StructureError("неожиданная часть")
    mime = f"{part[0].lower()}/{part[1].lower()}"
    size = part[6] if isinstance(part[6], int) else 0
    name = _param_value(part[2], "name") or ""
    for item in part[7:]:
        if isinstance(item, list) and item and isinstance(item[0], str) and item[0].lower() in ("attachment", "inline"):
            name = _param_value(item[1] if len(item) > 1 else None, "filename") or name
    return Attachment(mime_type=mime, name=sanitize_for_terminal(name)[:120], size=size)


def attachments_from_structure(tree: Any, depth: int = 0) -> list[Attachment]:
    if depth > MAX_DEPTH:
        raise StructureError("слишком глубокая структура")
    if not isinstance(tree, list) or not tree:
        raise StructureError("пустая структура")
    if isinstance(tree[0], list):
        found: list[Attachment] = []
        for child in tree:
            if not isinstance(child, list):
                break  # подтип multipart (например "mixed"): дальше параметры, а не части
            if child and isinstance(child[0], list):
                found.extend(attachments_from_structure(child, depth + 1))
            else:
                found.append(_single_part(child))
        return found
    return [_single_part(tree)]


KIND_TYPES = {"pdf": "application/pdf", "png": "image/png"}


def matches_kind(item: Attachment, kind: str) -> bool:
    if kind == "all":
        return item.is_candidate
    return item.mime_type == KIND_TYPES[kind] or item.name.lower().endswith(f".{kind}")


def candidate_attachments(meta: bytes, kind: str = "all", min_bytes: int = 0) -> list[Attachment] | None:
    """Кандидаты PNG/PDF. None — структуру не удалось разобрать (письмо нужно проверить целиком).

    min_bytes отсекает мелкие иконки рассылок: фото чеков обычно крупнее.
    """
    try:
        attachments = attachments_from_structure(parse_fetch_structure(meta))
    except StructureError:
        return None
    return [item for item in attachments if matches_kind(item, kind) and item.size >= min_bytes]
