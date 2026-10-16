from __future__ import annotations

import pytest

import mail_assistant.__main__ as cli
from helpers import build_email
from mail_assistant.imap_client import (
    ImapSession,
    MailError,
    decode_modified_utf7,
    imap_quote,
    parse_list_response,
    validate_mailbox_id,
)
from mail_assistant.__main__ import main
from mail_assistant.parser import parse_email

RUSSIAN_ID = "&BB8EOARBBEwEPAQwACAEQQQ1BDEENQ-"  # modified UTF-7 для «Письма себе»


def _select_index(server, name: str) -> int:
    for index, call in enumerate(server.calls):
        if call[0] == "SELECT" and call[1] == name:
            return index
    raise AssertionError(f"SELECT {name} не найден")


def test_russian_folder_name_is_decoded_from_modified_utf7():
    assert decode_modified_utf7(RUSSIAN_ID) == "Письма себе"


def test_ampersand_is_escaped_in_modified_utf7():
    assert decode_modified_utf7("Tom &- Jerry") == "Tom & Jerry"


def test_invalid_modified_utf7_is_returned_unchanged():
    assert decode_modified_utf7("&!!!-") == "&!!!-"


def test_list_parses_quoted_names_nil_delimiter_and_flags():
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) NIL "Sent Items"',
        b'(\\Noselect \\HasChildren) "/" "Parent"',
        f'(\\HasNoChildren) "/" "{RUSSIAN_ID}"'.encode(),
    ]

    infos = parse_list_response(data)

    assert [info.mailbox_id for info in infos] == ["INBOX", "Sent Items", "Parent", RUSSIAN_ID]
    assert infos[1].selectable is True
    assert infos[2].selectable is False
    assert infos[3].display_name == "Письма себе"


def test_list_unescapes_quotes_and_backslashes_in_names():
    data = [rb'(\HasNoChildren) "/" "a\"b\\c"']

    infos = parse_list_response(data)

    assert infos[0].mailbox_id == 'a"b\\c'


def test_list_literal_name_is_supported():
    data = [(b'(\\HasNoChildren) "/" {10}', b"Sent Items"), b")"]

    infos = parse_list_response(data)

    assert [info.mailbox_id for info in infos] == ["Sent Items"]


def test_malformed_list_line_is_skipped():
    assert parse_list_response([b"garbage line without structure"]) == []


def test_noselect_folder_is_marked_not_selectable():
    info = parse_list_response([b'(\\Noselect) "/" "Group"'])[0]

    assert info.selectable is False


@pytest.mark.parametrize(
    "value",
    ["INBOX\r\nSTORE 1 +FLAGS (\\Seen)", "INBOX\n", "IN\rBOX", "x\x00y", "", " INBOX", "INBOX ", "Письма", "a" * 201],
)
def test_bad_mailbox_values_are_rejected(value):
    with pytest.raises(MailError):
        validate_mailbox_id(value)


def test_plain_and_spaced_names_are_accepted():
    assert validate_mailbox_id("INBOX") == "INBOX"
    assert validate_mailbox_id("Sent Items") == "Sent Items"


def test_quoting_escapes_quotes_and_backslashes():
    assert imap_quote('a "b" \\c') == '"a \\"b\\" \\\\c"'


def test_selected_folder_is_quoted_and_opened_readonly(fake_server, mail_settings):
    fake_server.create("Sent Items")
    fake_server.add(7, build_email(), mailbox="Sent Items")

    with ImapSession(mail_settings, "Sent Items") as session:
        info, count = session.open_mailbox()
        assert session.recent_uids(5) == [7]

    assert info.mailbox_id == "Sent Items"
    assert count == 1
    select = next(call for call in fake_server.calls if call[0] == "SELECT")
    assert select[1:] == ("Sent Items", True, '"Sent Items"')


def test_russian_folder_is_selected_by_id_before_uid_fetch(fake_server, mail_settings):
    fake_server.create(RUSSIAN_ID)
    fake_server.add(500, build_email(subject="Из папки"), mailbox=RUSSIAN_ID)

    with ImapSession(mail_settings, RUSSIAN_ID) as session:
        raw = session.fetch_message(500)

    assert parse_email(raw).subject == "Из папки"
    select_at = _select_index(fake_server, RUSSIAN_ID)
    fetch_at = next(i for i, call in enumerate(fake_server.calls) if call[0] == "UID" and call[1] == "FETCH")
    assert select_at < fetch_at
    assert fake_server.calls[select_at][2] is True  # readonly=True
    peek_fetches = [call for call in fake_server.calls if call[0] == "UID" and "BODY.PEEK[]" in call[3]]
    assert peek_fetches


def test_uid_from_inbox_is_not_found_in_another_folder(fake_server, mail_settings):
    fake_server.add(101, build_email(subject="Только INBOX"))
    fake_server.create("Sent Items")

    with ImapSession(mail_settings, "Sent Items") as session:
        with pytest.raises(MailError, match="не найдено"):
            session.fetch_message(101)


def test_noselect_folder_is_refused_without_select(fake_server, mail_settings):
    fake_server.create("Group", flags="\\Noselect")

    with ImapSession(mail_settings, "Group") as session:
        with pytest.raises(MailError, match="Noselect"):
            session.open_mailbox()

    assert not any(call[0] == "SELECT" for call in fake_server.calls)


def test_unknown_folder_is_reported(fake_server, mail_settings):
    with ImapSession(mail_settings, "Nope") as session:
        with pytest.raises(MailError, match="не найдена"):
            session.open_mailbox()


def test_default_mailbox_is_inbox_and_keeps_old_behaviour(fake_server, mail_env, capsys):
    fake_server.add(101, build_email())

    assert main(["mail", "check"]) == 0

    out = capsys.readouterr().out
    assert "INBOX" in out
    assert ("SELECT", "INBOX", True, '"INBOX"') in fake_server.calls


def test_mail_list_shows_selected_folder(fake_server, mail_env, capsys):
    fake_server.create(RUSSIAN_ID)
    fake_server.add(7, build_email(subject="В папке"), mailbox=RUSSIAN_ID)

    assert main(["mail", "list", "--mailbox", RUSSIAN_ID]) == 0

    out = capsys.readouterr().out
    assert f"Папка: Письма себе [id: {RUSSIAN_ID}]" in out
    assert "UID 7" in out


def test_mail_read_shows_folder_before_content(fake_server, mail_env, capsys):
    fake_server.create(RUSSIAN_ID)
    fake_server.add(9, build_email(plain="Текст"), mailbox=RUSSIAN_ID)

    assert main(["mail", "read", "--uid", "9", "--mailbox", RUSSIAN_ID]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("Папка: Письма себе")
    assert "Текст" in out


def test_cli_rejects_crlf_in_mailbox(mail_env):
    with pytest.raises(SystemExit):
        main(["mail", "check", "--mailbox", "INBOX\r\nSTORE"])


def test_mail_folders_lists_names_ids_and_noselect(fake_server, mail_env, capsys):
    fake_server.create(RUSSIAN_ID)
    fake_server.create("Group", flags="\\Noselect")

    assert main(["mail", "folders"]) == 0

    out = capsys.readouterr().out
    assert f"Письма себе [id: {RUSSIAN_ID}]" in out
    assert "недоступна для выбора" in out
    assert "Веб-группы без строки здесь не являются папками" in out
    assert not any(call[0] == "FETCH" or call[0] == "UID" for call in fake_server.calls)


def test_analyze_report_contains_folder(fake_server, mail_env, llm_env, monkeypatch, capsys):
    import json

    import httpx

    from mail_assistant.llm_client import LLMClient as RealClient

    fake_server.create(RUSSIAN_ID)
    fake_server.add(12, build_email(plain="Срок сдачи отчёта: 20 октября."), mailbox=RUSSIAN_ID)
    content = json.dumps(
        {
            "category": "work",
            "summary": "Отчёт.",
            "needs_reply": False,
            "tasks": [{"description": "Отчёт", "deadline": "20 октября", "evidence": "Срок сдачи отчёта: 20 октября."}],
        },
        ensure_ascii=False,
    )
    body = {"model": "test-model", "choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    monkeypatch.setattr(cli, "LLMClient", lambda settings: RealClient(settings, transport=transport))

    assert main(["analyze", "--uid", "12", "--model", "test-model", "--allow-cloud", "--mailbox", RUSSIAN_ID]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["mailbox"] == {"id": RUSSIAN_ID, "display_name": "Письма себе"}
