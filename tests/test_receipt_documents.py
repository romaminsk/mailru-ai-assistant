from __future__ import annotations

import pytest
from PIL import Image

from receipt_fixtures import FakeOcr, UnavailableOcr, receipt_settings
from synthetic_docs import make_encrypted_pdf, make_pdf, make_png
from mail_assistant.documents import DocumentError, extract_document


def _extract(data, settings, ocr):
    return extract_document(data, settings, ocr)


def test_text_pdf_uses_embedded_text_without_ocr(tmp_path):
    ocr = FakeOcr()
    doc = _extract(make_pdf([{"lines": RECEIPT_LINES}]), receipt_settings(tmp_path), ocr)

    assert ocr.calls == 0
    assert doc.page_count == 1
    assert doc.pages[0].source == "pdf_text"
    assert "ITOGO 900.00 RUB" in doc.combined_text()
    assert "стр. 1, источник: pdf_text" in doc.combined_text()


def test_scanned_pdf_is_rendered_and_ocred(tmp_path):
    ocr = FakeOcr("ИТОГО 500,00 RUB")
    doc = _extract(make_pdf([{"image": (60, 80)}]), receipt_settings(tmp_path), ocr)

    assert ocr.calls == 1
    assert doc.pages[0].source == "ocr"
    assert "500,00" in doc.pages[0].text


def test_mixed_pdf_keeps_page_numbers_and_sources(tmp_path):
    ocr = FakeOcr("Второй чек ИТОГО 42,00 RUB")
    pages = [{"lines": RECEIPT_LINES}, {"image": (60, 80)}]
    doc = _extract(make_pdf(pages), receipt_settings(tmp_path), ocr)

    assert [(page.number, page.source) for page in doc.pages] == [(1, "pdf_text"), (2, "ocr")]
    assert ocr.calls == 1


def test_png_is_detected_by_content_not_name(tmp_path):
    ocr = FakeOcr("ИТОГО 900,00 RUB")
    doc = _extract(make_png(), receipt_settings(tmp_path), ocr)

    assert doc.mime_type == "image/png"
    assert doc.pages[0].source == "ocr"
    assert ocr.calls == 1


def test_scanned_input_without_ocr_is_an_explicit_error_not_fake_data(tmp_path):
    settings = receipt_settings(tmp_path)
    with pytest.raises(DocumentError) as excinfo:
        _extract(make_png(), settings, UnavailableOcr())
    assert excinfo.value.code == "OCR_UNAVAILABLE"
    assert "Tesseract" in str(excinfo.value)

    with pytest.raises(DocumentError) as excinfo:
        _extract(make_pdf([{"image": (60, 80)}]), settings, UnavailableOcr())
    assert excinfo.value.code == "OCR_UNAVAILABLE"


def test_text_pdf_does_not_need_ocr_even_if_ocr_is_missing(tmp_path):
    doc = _extract(make_pdf([{"lines": RECEIPT_LINES}]), receipt_settings(tmp_path), UnavailableOcr())

    assert doc.pages[0].source == "pdf_text"


def test_corrupt_files_get_clear_codes(tmp_path):
    settings = receipt_settings(tmp_path)
    with pytest.raises(DocumentError) as excinfo:
        _extract(b"%PDF-1.4 broken garbage without structure", settings, FakeOcr())
    assert excinfo.value.code in {"FILE_CORRUPT", "NO_TEXT"}

    with pytest.raises(DocumentError) as excinfo:
        _extract(b"\x89PNG\r\n\x1a\n" + b"not really an image", settings, FakeOcr())
    assert excinfo.value.code == "FILE_CORRUPT"


def test_unsupported_content_is_rejected(tmp_path):
    with pytest.raises(DocumentError) as excinfo:
        _extract(b"GIF89a....", receipt_settings(tmp_path), FakeOcr())
    assert excinfo.value.code == "UNSUPPORTED_TYPE"


def test_encrypted_pdf_is_not_decrypted(tmp_path):
    with pytest.raises(DocumentError) as excinfo:
        _extract(make_encrypted_pdf(), receipt_settings(tmp_path), FakeOcr())
    assert excinfo.value.code == "PDF_ENCRYPTED"


def test_too_many_pages_is_rejected(tmp_path):
    settings = receipt_settings(tmp_path, max_pages=1)
    with pytest.raises(DocumentError) as excinfo:
        _extract(make_pdf([{"lines": RECEIPT_LINES}, {"lines": RECEIPT_LINES}]), settings, FakeOcr())
    assert excinfo.value.code == "TOO_MANY_PAGES"


def test_too_large_file_is_rejected_before_parsing(tmp_path):
    settings = receipt_settings(tmp_path, max_bytes=1024)
    with pytest.raises(DocumentError) as excinfo:
        _extract(make_png(400, 400), settings, FakeOcr())
    assert excinfo.value.code == "FILE_TOO_LARGE"


def test_image_pixel_limit_blocks_large_png(tmp_path):
    settings = receipt_settings(tmp_path, max_pixels=1000)
    with pytest.raises(DocumentError) as excinfo:
        _extract(make_png(100, 100), settings, FakeOcr())
    assert excinfo.value.code == "IMAGE_TOO_LARGE"


def test_pillow_decompression_bomb_protection_is_not_disabled(tmp_path):
    original = Image.MAX_IMAGE_PIXELS
    _extract(make_png(), receipt_settings(tmp_path), FakeOcr())

    assert Image.MAX_IMAGE_PIXELS == original
    assert Image.MAX_IMAGE_PIXELS is not None


def test_ocr_timeout_is_reported_with_code(tmp_path):
    class TimeoutOcr(FakeOcr):
        def image_to_text(self, image, psm=4):
            from mail_assistant.ocr import OcrError

            raise OcrError("Время OCR истекло", "OCR_TIMEOUT")

    with pytest.raises(DocumentError) as excinfo:
        _extract(make_png(), receipt_settings(tmp_path), TimeoutOcr())
    assert excinfo.value.code == "OCR_TIMEOUT"


RECEIPT_LINES = [
    "MAGAZIN TEST LLC",
    "INN 7701234567",
    "05.10.2026 14:32",
    "SALE 1000.00 RUB",
    "VAT 20% 166.67",
    "DISCOUNT -100.00",
    "ITOGO 900.00 RUB",
    "CASH 1000.00",
    "CHANGE 100.00",
]
