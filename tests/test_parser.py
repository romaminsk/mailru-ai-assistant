from __future__ import annotations

from helpers import build_email
from mail_assistant.parser import (
    html_to_text,
    normalize_for_match,
    parse_email,
    sanitize_for_terminal,
    truncate_text,
)


def test_russian_subject_and_sender_are_decoded():
    parsed = parse_email(build_email(subject="Счёт за услуги №42", sender="Мария Сидорова <maria@example.com>"))

    assert parsed.subject == "Счёт за услуги №42"
    assert parsed.sender == "Мария Сидорова <maria@example.com>"
    assert parsed.date.startswith("Mon, 05 Oct 2026")


def test_plain_text_is_preferred_and_attachments_are_listed_not_opened():
    raw = build_email(
        plain="Текст письма: срок до 15 октября.",
        html="<p>HTML-версия</p>",
        attachments=(("report.pdf", b"%PDF-1.4 fake"),),
    )

    parsed = parse_email(raw)

    assert parsed.body_kind == "plain"
    assert "срок до 15 октября" in parsed.body_text
    assert "HTML-версия" not in parsed.body_text
    assert parsed.attachments == ("report.pdf",)
    assert "%PDF" not in parsed.body_text


def test_html_only_message_uses_fallback_without_script_and_style():
    html = (
        "<html><head><style>p { color: red; }</style></head><body>"
        "<script>alert('boom')</script><p>Срок: 20 октября</p>"
        "<div>Оплата 5000 рублей</div></body></html>"
    )

    parsed = parse_email(build_email(plain=None, html=html))

    assert parsed.body_kind == "html"
    assert "Срок: 20 октября" in parsed.body_text
    assert "Оплата 5000 рублей" in parsed.body_text
    assert "alert" not in parsed.body_text
    assert "color" not in parsed.body_text


def test_legacy_cp1251_body_is_decoded():
    raw = (
        b"From: Anna <anna@example.com>\r\n"
        + "Subject: Тема в cp1251\r\n".encode("cp1251")
        + b"Date: Mon, 05 Oct 2026 10:00:00 +0300\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=windows-1251\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        + "Привет, срок до 15 октября".encode("cp1251")
    )

    parsed = parse_email(raw)

    assert "срок до 15 октября" in parsed.body_text
    assert parsed.subject == "Тема в cp1251"


def test_broken_headers_do_not_crash():
    raw = (
        b"From: =?utf-8?B?!!!broken?=\r\n"
        b"Subject: =?unknown-charset?Q?abc?=\r\n"
        b"Date: not a date at all\r\n\r\n"
        b"Body text\r\n"
    )

    parsed = parse_email(raw)

    assert parsed.body_text == "Body text"
    assert isinstance(parsed.subject, str)


def test_terminal_sequences_and_bidi_controls_are_removed():
    dirty = "\x1b[2J\x1b]0;evil-title\x07Привет\u202e\x00\x07 мир"

    cleaned = sanitize_for_terminal(dirty)

    assert cleaned == "Привет мир"
    assert "\x1b" not in cleaned
    assert "\u202e" not in cleaned


def test_multiline_sanitization_keeps_line_breaks():
    assert sanitize_for_terminal("строка 1\r\n\r\n\r\n\x1b[31mстрока 2", multiline=True) == "строка 1\n\nстрока 2"


def test_truncate_marks_long_text():
    assert truncate_text("а" * 10, 5) == ("а" * 5, True)
    assert truncate_text("abc", 5) == ("abc", False)


def test_html_to_text_is_safe_on_malformed_markup():
    assert "текст" in html_to_text("<p>текст<<<<<<")


def test_normalization_handles_quotes_dashes_and_case():
    assert normalize_for_match("Срок — «20 Октября»") == normalize_for_match('срок - "20 октября"')
