from __future__ import annotations

import ssl

import pytest

from helpers import PASSWORD, USER, build_email
from mail_assistant.config import MailSettings
from mail_assistant.imap_client import HEADER_ITEMS, ImapSession, MailError, validate_uid

WRITE_COMMANDS = {"STORE", "MOVE", "EXPUNGE", "APPEND", "SEARCH", "SORT", "COPY"}


def _commands_text(server) -> str:
    return " ".join(str(part) for call in server.calls for part in call)


def test_check_opens_inbox_readonly_and_logs_out(fake_server, mail_settings):
    fake_server.add(101, build_email(subject="A"))
    fake_server.add(102, build_email(subject="B"))

    with ImapSession(mail_settings) as session:
        _, count = session.open_mailbox()

    assert count == 2
    assert ("SELECT", "INBOX", True, '"INBOX"') in fake_server.calls
    assert fake_server.calls[-1] == ("LOGOUT",)


def test_connection_uses_verified_tls_context(fake_server, mail_settings):
    with ImapSession(mail_settings):
        pass

    conn = fake_server.connections[0]
    assert conn["host"] == "imap.mail.ru"
    assert conn["port"] == 993
    assert conn["timeout"] == 5
    assert isinstance(conn["ssl_context"], ssl.SSLContext)
    assert conn["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert conn["ssl_context"].check_hostname is True


def test_no_write_commands_are_sent(fake_server, mail_settings):
    fake_server.add(101, build_email())
    with ImapSession(mail_settings) as session:
        session.recent_headers(5)
        session.fetch_message(101)

    commands = {str(call[0]) for call in fake_server.calls}
    assert not commands & WRITE_COMMANDS
    assert "STORE" not in _commands_text(fake_server)


def test_empty_mailbox_returns_no_rows(fake_server, mail_settings):
    with ImapSession(mail_settings) as session:
        assert session.recent_headers(5) == []

    assert not any(call[0] == "FETCH" for call in fake_server.calls)


def test_list_returns_last_messages_in_mailbox_order(fake_server, mail_settings):
    for uid in range(101, 108):
        fake_server.add(uid, build_email(subject=f"Письмо {uid}"))

    with ImapSession(mail_settings) as session:
        rows = session.recent_headers(5)

    assert [uid for uid, _ in rows] == [103, 104, 105, 106, 107]
    assert rows[0][1].subject == "Письмо 103"
    assert rows[0][1].sender == "Иван Петров <ivan@example.com>"


def test_list_uses_peek_headers_by_uid_and_no_search(fake_server, mail_settings):
    fake_server.add(101, build_email())

    with ImapSession(mail_settings) as session:
        session.recent_headers(5)

    uid_fetches = [call for call in fake_server.calls if call[0] == "UID" and call[1] == "FETCH"]
    assert uid_fetches
    assert uid_fetches[0][3] == HEADER_ITEMS
    assert "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]" in HEADER_ITEMS
    assert not any(call[0] in {"SEARCH", "SORT"} for call in fake_server.calls)


def test_vanished_message_is_reported_as_missing_not_crash(fake_server, mail_settings):
    fake_server.add(101, build_email(subject="Есть"))
    fake_server.order.append(102)  # сообщение видно по порядковому номеру, но уже удалено

    with ImapSession(mail_settings) as session:
        rows = session.recent_headers(5)

    assert rows[0][1] is not None
    assert rows[1] == (102, None)


def test_read_checks_size_then_fetches_with_peek(fake_server, mail_settings):
    raw = build_email(plain="Срок сдачи: 20 октября.")
    fake_server.add(101, raw)

    with ImapSession(mail_settings) as session:
        assert session.fetch_message(101) == raw

    assert ("SELECT", "INBOX", True, '"INBOX"') in fake_server.calls
    fetches = [call for call in fake_server.calls if call[0] == "UID" and call[1] == "FETCH"]
    assert fetches[0][3] == "(RFC822.SIZE)"
    assert fetches[1][3] == "(BODY.PEEK[])"


def test_oversized_message_is_refused_before_body_download(fake_server, mail_settings):
    fake_server.add(101, build_email(plain="x" * 5000))
    small_limit = MailSettings(
        username=USER,
        app_password=PASSWORD,
        host="imap.mail.ru",
        port=993,
        timeout_seconds=5,
        max_message_bytes=1024,
    )

    with ImapSession(small_limit) as session:
        with pytest.raises(MailError, match="слишком большое"):
            session.fetch_message(101)

    assert not any("BODY.PEEK[]" in str(call) for call in fake_server.calls)


def test_missing_uid_gives_controlled_error(fake_server, mail_settings):
    fake_server.add(101, build_email())

    with ImapSession(mail_settings) as session:
        with pytest.raises(MailError, match="не найдено"):
            session.fetch_message(999)


def test_logout_error_does_not_hide_original_error(fake_server, mail_settings):
    fake_server.logout_error = OSError("socket already closed")

    with pytest.raises(MailError, match="не найдено"):
        with ImapSession(mail_settings) as session:
            session.fetch_message(999)

    assert ("SHUTDOWN",) in fake_server.calls


def test_wrong_password_message_does_not_contain_password(fake_server, mail_settings):
    wrong = MailSettings(
        username=USER,
        app_password="wrong-password-xyz",
        host="imap.mail.ru",
        port=993,
        timeout_seconds=5,
        max_message_bytes=1000,
    )

    with pytest.raises(MailError) as excinfo:
        with ImapSession(wrong):
            pass

    assert "wrong-password-xyz" not in str(excinfo.value)


def test_missing_credentials_are_reported_without_connecting(fake_server):
    empty = MailSettings(
        username="",
        app_password="",
        host="imap.mail.ru",
        port=993,
        timeout_seconds=5,
        max_message_bytes=1000,
    )
    from mail_assistant.config import AppError

    with pytest.raises(AppError, match="MAIL_USERNAME"):
        with ImapSession(empty):
            pass
    assert fake_server.connections == []


@pytest.mark.parametrize("bad", [0, -1, 2**32, True, "12"])
def test_uid_validation_rejects_bad_values(bad):
    with pytest.raises(MailError):
        validate_uid(bad)  # type: ignore[arg-type]
