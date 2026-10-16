"""Реальный Tesseract. Пропускается, если Tesseract или языки rus/eng не установлены."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from receipt_fixtures import FakeLLM, completion, llm_settings, receipt_settings
from synthetic_docs import make_rgb_scan_pdf
from mail_assistant.ocr import OcrError, TesseractOcr
from mail_assistant.receipts import AiOptions, LLMBudget, SourceRef, confirm_receipt, process_document
from mail_assistant.report import build_report
from mail_assistant.storage import Store

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
RECEIPT_LINES = [
    "МАГАЗИН ТЕСТ ООО",
    "ИНН 7701234567",
    "05.10.2026 14:32",
    "ПРОДАЖА 1000.00 RUB",
    "НДС 20% 166.67",
    "СКИДКА -100.00",
    "ИТОГО 900.00 RUB",
    "СДАЧА 100.00",
]


def _tesseract_ready() -> bool:
    try:
        TesseractOcr("rus+eng", 30).ensure_available()
        return True
    except OcrError:
        return False


def _font():
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, 28)
    return None


pytestmark = pytest.mark.skipif(not _tesseract_ready(), reason="Tesseract или языки rus/eng не установлены")


def _receipt_image() -> Image.Image:
    font = _font()
    if font is None:
        pytest.skip("нет шрифта с кириллицей")
    image = Image.new("RGB", (760, 60 + 42 * len(RECEIPT_LINES)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(RECEIPT_LINES):
        draw.text((30, 30 + index * 42), line, fill="black", font=font)
    return image


def test_real_tesseract_reads_synthetic_receipt_png(tmp_path):
    image = _receipt_image()
    text = TesseractOcr("rus+eng", 60).image_to_text(image)

    assert "900" in text
    assert "7701234567" in text
    assert "RUB" in text


def test_real_scan_pdf_goes_through_render_and_ocr(tmp_path):
    data = make_rgb_scan_pdf(_receipt_image())
    settings = receipt_settings(tmp_path, render_dpi=150, min_page_text_chars=20)
    with Store(settings.db_path) as store:
        result = process_document(
            store, data, source=SourceRef(part_name="scan.pdf"), settings=settings,
            ocr=TesseractOcr("rus+eng", 60),
        )
        document = store.document(result.document_id)

    assert result.error_code is None
    assert document["page_count"] == 1
    assert "900" in document["extracted_text"]
    assert "--- стр. 1, источник: ocr ---" in document["extracted_text"]


def test_real_ocr_text_passes_verification_and_report(tmp_path):
    """Цепочка с реальным OCR; ответ LLM — mock, построенный из реально распознанного текста."""
    settings = receipt_settings(tmp_path)
    data = make_rgb_scan_pdf(_receipt_image())
    ocr = TesseractOcr("rus+eng", 60)
    with Store(settings.db_path) as store:
        local = process_document(store, data, source=SourceRef(part_name="scan.pdf"), settings=settings, ocr=ocr)
        text = store.document(local.document_id)["extracted_text"]
        total_line = next(line for line in text.splitlines() if "900" in line)
        content = {
            "receipts": [
                {
                    "merchant_name": "МАГАЗИН ТЕСТ ООО",
                    "merchant_tax_id": "7701234567",
                    "purchase_date": "2026-10-05",
                    "purchase_time": "14:32",
                    "currency": "RUB",
                    "total_amount": "900.00",
                    "operation_type": "expense",
                    "category": "food",
                    "receipt_number": None,
                    "fiscal_id": None,
                    "evidence": {
                        "merchant": "МАГАЗИН ТЕСТ ООО",
                        "date": "05.10.2026 14:32",
                        "amount": total_line.strip(),
                        "operation": "ПРОДАЖА 1000.00 RUB",
                    },
                    "warnings": [],
                }
            ]
        }
        fake = FakeLLM(lambda request: completion(json.dumps(content, ensure_ascii=False)))
        ai = AiOptions(allow_cloud=True, llm_settings=llm_settings(), budget=LLMBudget(1), client_factory=fake.factory)
        result = process_document(
            store, data, source=SourceRef(part_name="scan.pdf"), settings=settings, ocr=ocr, ai=ai, reprocess=True
        )
        assert result.status == "ai_done", result.error_code
        confirm_receipt(store, result.receipt_ids[0])
        report = build_report(store, "2026-10-01", "2026-10-31")

    assert report["currencies"]["RUB"]["expense"] == "900.00"
    assert len(fake.calls) == 1
