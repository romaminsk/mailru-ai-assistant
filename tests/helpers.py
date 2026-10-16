from __future__ import annotations

import email.policy
import imaplib
import re
from datetime import date, datetime
from email.message import EmailMessage
from typing import Any

USER = "user@mail.ru"
PASSWORD = "app-pass-456"
LLM_KEY = "test-key-123"
LLM_API_URL = "https://llm.example.test/v1/chat/completions"
LLM_MODELS_URL = "https://llm.example.test/v1/models"


def build_email(
    *,
    subject: str = "Тестовое письмо",
    sender: str = "Иван Петров <ivan@example.com>",
    date: str = "Mon, 05 Oct 2026 10:00:00 +0300",
    plain: str | None = "Привет! Срок сдачи отчёта: 20 октября.",
    html: str | None = None,
    attachments: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = USER
    msg["Subject"] = subject
    msg["Date"] = date
    if plain is not None and html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(plain or "")
    for filename, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    return msg.as_bytes(policy=email.policy.SMTP)


class FakeMailbox:
    def __init__(self, flags: str = "\\HasNoChildren") -> None:
        self.flags = flags
        self.order: list[int] = []
        self.messages: dict[int, bytes] = {}
        self.received: dict[int, date] = {}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FakeServer:
    """Синтетический IMAP-сервер: папки, UID в порядке папки, журнал всех команд."""

    def __init__(self) -> None:
        self.user = USER
        self.password = PASSWORD
        self.uidvalidity = 1000
        self.mailboxes: dict[str, FakeMailbox] = {"INBOX": FakeMailbox()}
        self.calls: list[tuple[Any, ...]] = []
        self.connections: list[dict[str, Any]] = []
        self.logout_error: BaseException | None = None

    @property
    def order(self) -> list[int]:
        return self.mailboxes["INBOX"].order

    @property
    def messages(self) -> dict[int, bytes]:
        return self.mailboxes["INBOX"].messages

    def create(self, name: str, flags: str = "\\HasNoChildren") -> FakeMailbox:
        self.mailboxes[name] = FakeMailbox(flags)
        return self.mailboxes[name]

    def add(self, uid: int, raw: bytes, mailbox: str = "INBOX", received: date = date(2026, 10, 5)) -> None:
        box = self.mailboxes[mailbox]
        box.order.append(uid)
        box.messages[uid] = raw
        box.received[uid] = received

    def commands(self) -> list[str]:
        return [str(call[0]) for call in self.calls]


def _structure_of(message) -> str:
    def leaf(part) -> str:
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        params = f'("name" "{filename}")' if filename else "NIL"
        disposition = f'("attachment" ("filename" "{filename}"))' if filename else "NIL"
        maintype, subtype = part.get_content_type().split("/", 1)
        return f'("{maintype}" "{subtype}" {params} NIL NIL "base64" {len(payload)} NIL {disposition} NIL NIL)'

    if message.is_multipart():
        children = "".join(_structure_of(child) for child in message.get_payload())
        return f'({children} "mixed" ("boundary" "x") NIL NIL NIL)'
    return leaf(message)


def bodystructure_for(raw: bytes) -> str:
    import email
    import email.policy

    message = email.message_from_bytes(raw, policy=email.policy.default)
    return _structure_of(message)


class FakeConnection:
    def __init__(self, server: FakeServer) -> None:
        self._server = server
        self._box: FakeMailbox = server.mailboxes["INBOX"]

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self._server.calls.append(("LOGIN", user))
        if user != self._server.user or password != self._server.password:
            raise imaplib.IMAP4.error("authentication failed")
        return "OK", [b"LOGIN completed"]

    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[bytes]]:
        self._server.calls.append(("LIST", directory, pattern))
        lines = [
            f'({box.flags}) "/" {_quote(name)}'.encode("utf-8")
            for name, box in self._server.mailboxes.items()
        ]
        return "OK", lines

    def response(self, code: str) -> tuple[str, list[bytes]]:
        if code == "UIDVALIDITY":
            return code, [str(self._server.uidvalidity).encode()]
        return code, []

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        name = _unquote(mailbox)
        self._server.calls.append(("SELECT", name, readonly, mailbox))
        if name not in self._server.mailboxes:
            return "NO", [b"no such mailbox"]
        self._box = self._server.mailboxes[name]
        return "OK", [str(len(self._box.order)).encode()]

    def fetch(self, message_set: str, items: str) -> tuple[str, list[bytes]]:
        self._server.calls.append(("FETCH", message_set, items))
        first, last = (int(part) for part in message_set.split(":"))
        data = []
        for seq in range(first, last + 1):
            if 1 <= seq <= len(self._box.order):
                data.append(f"{seq} (UID {self._box.order[seq - 1]})".encode())
        return "OK", data

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self._server.calls.append(("UID", command, *args))
        if command == "SEARCH":
            return "OK", [self._search(args)]
        uid_set, items = args
        if "BODYSTRUCTURE" in items:
            data_structure: list[Any] = []
            for uid_text in uid_set.split(","):
                uid = int(uid_text)
                raw = self._box.messages.get(uid)
                if raw is not None:
                    seq = self._box.order.index(uid) + 1
                    data_structure.append(f"{seq} (UID {uid} BODYSTRUCTURE {bodystructure_for(raw)})".encode())
            return "OK", data_structure
        data: list[Any] = []
        for uid_text in uid_set.split(","):
            uid = int(uid_text)
            raw = self._box.messages.get(uid)
            if raw is None:
                continue
            seq = self._box.order.index(uid) + 1
            if "RFC822.SIZE" in items:
                data.append(f"{seq} (UID {uid} RFC822.SIZE {len(raw)})".encode())
            elif "HEADER.FIELDS" in items:
                head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                data.append((f"{seq} (UID {uid} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{{len(head)}}}".encode(), head))
                data.append(b")")
            elif "BODY.PEEK[]" in items:
                data.append((f"{seq} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                data.append(b")")
        return "OK", data

    def _search(self, criteria: tuple[str, ...]) -> bytes:
        since = before = None
        for keyword, value in zip(criteria[::2], criteria[1::2]):
            parsed = datetime.strptime(value, "%d-%b-%Y").date()
            if keyword == "SINCE":
                since = parsed
            elif keyword == "BEFORE":
                before = parsed
        matched = [
            uid
            for uid in self._box.order
            if (since is None or self._box.received[uid] >= since)
            and (before is None or self._box.received[uid] < before)
        ]
        return " ".join(str(uid) for uid in matched).encode()

    def logout(self) -> tuple[str, list[bytes]]:
        self._server.calls.append(("LOGOUT",))
        if self._server.logout_error is not None:
            raise self._server.logout_error
        return "BYE", [b"logout"]

    def shutdown(self) -> None:
        self._server.calls.append(("SHUTDOWN",))
