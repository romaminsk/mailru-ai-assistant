from __future__ import annotations

from mail_assistant.bodystructure import candidate_attachments, parse_fetch_structure

NESTED = (
    b'1 (UID 9 BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 120 4 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "7bit" 300 9 NIL NIL NIL NIL) "alternative" ("boundary" "a") NIL NIL NIL)'
    b'("application" "pdf" ("name" "Check 1.pdf") NIL NIL "base64" 2048 NIL ("attachment" ("filename" "Check 1.pdf")) NIL NIL)'
    b'("image" "png" NIL "<img1>" NIL "base64" 512 NIL ("inline" NIL) NIL NIL)'
    b'("application" "vnd.ms-excel" ("name" "table.xls") NIL NIL "base64" 999 NIL ("attachment" ("filename" "table.xls")) NIL NIL)'
    b' "mixed" ("boundary" "m") NIL NIL NIL))'
)


def test_nested_multipart_yields_pdf_and_png_candidates_only():
    found = candidate_attachments(NESTED)

    assert found is not None
    assert [(item.mime_type, item.name, item.size) for item in found] == [
        ("application/pdf", "Check 1.pdf", 2048),
        ("image/png", "", 512),
    ]


def test_single_part_pdf_attachment_is_detected_by_name_when_type_is_generic():
    meta = b'3 (UID 11 BODYSTRUCTURE ("application" "octet-stream" ("name" "scan.PDF") NIL NIL "base64" 77 NIL NIL NIL NIL))'

    found = candidate_attachments(meta)

    assert found is not None and found[0].name == "scan.PDF" and found[0].is_candidate


def test_plain_text_message_has_no_candidates():
    meta = b'4 (UID 12 BODYSTRUCTURE ("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 40 2 NIL NIL NIL NIL))'

    assert candidate_attachments(meta) == []


def test_non_receipt_attachment_is_not_candidate():
    meta = (
        b'5 (UID 13 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 5 1 NIL NIL NIL NIL)'
        b'("application" "zip" ("name" "a.zip") NIL NIL "base64" 9 NIL ("attachment" ("filename" "a.zip")) NIL NIL) "mixed" NIL NIL NIL NIL))'
    )

    assert candidate_attachments(meta) == []


def test_broken_structure_is_unknown_not_silent_skip():
    assert candidate_attachments(b"6 (UID 14 BODYSTRUCTURE ((\"text\" \"plain\"") is None
    assert candidate_attachments(b"7 (UID 15 FLAGS (\\Seen))") is None


def test_quoted_names_with_escapes_and_spaces():
    meta = rb'8 (UID 16 BODYSTRUCTURE ("application" "pdf" ("name" "my \"chek\" 1.pdf") NIL NIL "base64" 5 NIL NIL NIL NIL))'

    found = candidate_attachments(meta)

    assert found is not None and found[0].name == 'my "chek" 1.pdf'


def test_structure_parser_returns_tree_for_fetch_line():
    tree = parse_fetch_structure(b'9 (UID 17 BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" 1 1 NIL NIL NIL NIL))')

    assert tree[0] == "text" and tree[1] == "plain"
