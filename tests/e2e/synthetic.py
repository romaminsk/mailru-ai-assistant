"""Синтетические документы и эталон для сквозных сценариев.

Все реквизиты вымышленные. Каждый документ содержит строку «Метка: СЛОВО» (длинное русское слово,
которое OCR читает надёжно). Метка нужна только подставному LLM, чтобы выбрать заранее заданный ответ.
Текст документа читается настоящим извлечением текста (PDF) и настоящим Tesseract OCR (PNG и скан).

Эталон (EXPECTED) посчитан вручную и не выводится из приложения.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
)

Fmt = Literal["png", "pdf-text", "pdf-scan"]


def font_path() -> Path:
    """Шрифт с кириллицей. Путь можно задать E2E_FONT_PATH; иначе ищется Arial."""
    raw = os.environ.get("E2E_FONT_PATH")
    candidates = [Path(raw)] if raw else [Path(item) for item in FONT_CANDIDATES]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Не найден шрифт с кириллицей. Задайте E2E_FONT_PATH (например Arial.ttf).")


def item(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "merchant_name": None, "merchant_tax_id": None, "purchase_date": None, "purchase_time": None,
        "currency": None, "total_amount": None, "operation_type": "unknown", "category": "other",
        "receipt_number": None, "fiscal_id": None, "document_kind": "unknown", "payment_status": "unknown",
        "intermediary": None, "purpose": None, "description": None, "fee_amount": None, "debit_amount": None,
        "evidence": {}, "warnings": [],
    }
    base.update(fields)
    return base


def answer(*items: dict[str, Any]) -> dict[str, Any]:
    return {"receipts": list(items)}


@dataclass(frozen=True)
class Doc:
    key: str
    filename: str
    fmt: Fmt
    marker: str
    lines: tuple[str, ...]
    answer: dict[str, Any] = field(repr=False)


LOAN_LINES = (
    "Получатель: ЗАО ТЕСТ КРЕДИТ",
    "Дата платежа: 05.10.2026 14:32",
    "Назначение: Погашение кредита частичное",
    "Номер операции в ЕРИП: 700112233",
    "Итого: 2488.00 BYN",
)
LOAN_ANSWER = answer(item(
    merchant_name="ЗАО ТЕСТ КРЕДИТ", purchase_date="2026-10-05", purchase_time="14:32", currency="BYN",
    total_amount="2488.00", operation_type="expense", category="loan", document_kind="payment",
    payment_status="paid", intermediary="ЕРИП", purpose="Погашение кредита частичное",
    description="Частичное погашение кредита", debit_amount="2488.00",
    evidence={"merchant": "ЗАО ТЕСТ КРЕДИТ", "date": "05.10.2026 14:32", "amount": "2488.00",
              "operation": "Назначение: Погашение кредита частичное", "purpose": "Назначение: Погашение кредита частичное"},
))

DOCS: tuple[Doc, ...] = (
    Doc(
        key="loan_png", filename="e2e_loan_payment.png", fmt="png", marker="АЛЬФА",
        lines=("КВИТАНЦИЯ ОБ ОПЛАТЕ ЕРИП",) + LOAN_LINES + ("Метка: АЛЬФА",),
        answer=LOAN_ANSWER,
    ),
    Doc(
        key="loan_pdf_copy", filename="e2e_loan_payment_copy.pdf", fmt="pdf-text", marker="АЛЬФА",
        lines=("ЕРИП: подтверждение платежа",) + LOAN_LINES + ("Метка: АЛЬФА",),
        answer=LOAN_ANSWER,
    ),
    Doc(
        key="loan_second_png", filename="e2e_loan_second.png", fmt="png", marker="БРАВО",
        lines=(
            "КВИТАНЦИЯ ОБ ОПЛАТЕ ЕРИП",
            "Получатель: ЗАО ТЕСТ КРЕДИТ",
            "Дата платежа: 05.10.2026 16:05",
            "Назначение: Погашение кредита частичное",
            "Номер операции в ЕРИП: 700998877",
            "Итого: 2488.00 BYN",
            "Метка: БРАВО",
        ),
        answer=answer(item(
            merchant_name="ЗАО ТЕСТ КРЕДИТ", purchase_date="2026-10-05", purchase_time="16:05", currency="BYN",
            total_amount="2488.00", operation_type="expense", category="loan", document_kind="payment",
            payment_status="paid", intermediary="ЕРИП", purpose="Погашение кредита частичное",
            debit_amount="2488.00",
            evidence={"merchant": "ЗАО ТЕСТ КРЕДИТ", "date": "05.10.2026 16:05", "amount": "2488.00",
                      "operation": "Назначение: Погашение кредита частичное", "purpose": "Назначение: Погашение кредита частичное"},
        )),
    ),
    Doc(
        key="service_pdf", filename="e2e_service_fee.pdf", fmt="pdf-text", marker="ОРИОН",
        lines=(
            "Оплата услуг связи",
            "Получатель: ОАО ТЕСТ СВЯЗЬ",
            "Дата: 08.10.2026 10:15",
            "Услуга интернет за сентябрь 2026: 55.00 BYN",
            "Комиссия: 2.00 BYN",
            "Итого списано: 57.00 BYN",
            "Номер операции в ЕРИП: 700445566",
            "Метка: ОРИОН",
        ),
        answer=answer(item(
            merchant_name="ОАО ТЕСТ СВЯЗЬ", purchase_date="2026-10-08", purchase_time="10:15", currency="BYN",
            total_amount="57.00", operation_type="expense", category="services", document_kind="payment",
            payment_status="paid", intermediary="ЕРИП", purpose="Интернет за сентябрь 2026",
            fee_amount="2.00", debit_amount="57.00",
            evidence={"merchant": "ОАО ТЕСТ СВЯЗЬ", "date": "08.10.2026 10:15", "amount": "57.00",
                      "operation": "Оплата услуг связи", "purpose": "Услуга интернет за сентябрь 2026",
                      "fee": "2.00"},
        )),
    ),
    Doc(
        key="store_scan", filename="e2e_store_scan.pdf", fmt="pdf-scan", marker="ГАММА",
        lines=(
            "ТЕСТМАРКЕТ",
            "Дата покупки: 11.10.2026 18:40",
            "Покупка: продукты",
            "Итого: 31.40 BYN",
            "Метка: ГАММА",
        ),
        answer=answer(item(
            merchant_name="ТЕСТМАРКЕТ", purchase_date="2026-10-11", purchase_time="18:40", currency="BYN",
            total_amount="31.40", operation_type="expense", category="food", document_kind="receipt",
            payment_status="unknown",
            evidence={"merchant": "ТЕСТМАРКЕТ", "date": "11.10.2026 18:40", "amount": "31.40",
                      "operation": "Покупка: продукты"},
        )),
    ),
    Doc(
        key="invoice_pdf", filename="e2e_invoice_unpaid.pdf", fmt="pdf-text", marker="ДЕЛЬТА",
        lines=(
            "Счёт № 4471 на оплату",
            "Поставщик: ООО ТЕСТ ЭНЕРГО",
            "Дата счёта: 03.10.2026",
            "Назначение: Электроэнергия за сентябрь",
            "К оплате: 300.00 BYN",
            "Статус: не оплачен",
            "Метка: ДЕЛЬТА",
        ),
        answer=answer(item(
            merchant_name="ООО ТЕСТ ЭНЕРГО", purchase_date="2026-10-03", currency="BYN", total_amount="300.00",
            operation_type="expense", category="utilities", document_kind="invoice", payment_status="unpaid",
            purpose="Электроэнергия за сентябрь",
            evidence={"merchant": "ООО ТЕСТ ЭНЕРГО", "date": "Дата счёта: 03.10.2026", "amount": "300.00",
                      "operation": "Счёт № 4471 на оплату", "purpose": "Назначение: Электроэнергия за сентябрь"},
        )),
    ),
    Doc(
        key="refund_png", filename="e2e_refund_usd.png", fmt="png", marker="ОМИКРОН",
        lines=(
            "ВОЗВРАТ ПЛАТЕЖА",
            "Получатель: TEST SHOP INC",
            "Дата: 10.10.2026 12:00",
            "Возврат: 25.00 USD",
            "Итого: 25.00 USD",
            "Метка: ОМИКРОН",
        ),
        answer=answer(item(
            merchant_name="TEST SHOP INC", purchase_date="2026-10-10", purchase_time="12:00", currency="USD",
            total_amount="25.00", operation_type="refund", category="other", document_kind="receipt",
            evidence={"merchant": "TEST SHOP INC", "date": "10.10.2026 12:00", "amount": "25.00",
                      "operation": "ВОЗВРАТ ПЛАТЕЖА"},
        )),
    ),
    Doc(
        key="usd_png", filename="e2e_usd_expense.png", fmt="png", marker="ТРИТОН",
        lines=(
            "ЧЕК ОПЛАТЫ",
            "Получатель: TEST SHOP INC",
            "Дата: 09.10.2026 11:20",
            "Покупка: подписка",
            "Итого: 12.50 USD",
            "Метка: ТРИТОН",
        ),
        answer=answer(item(
            merchant_name="TEST SHOP INC", purchase_date="2026-10-09", purchase_time="11:20", currency="USD",
            total_amount="12.50", operation_type="expense", category="entertainment", document_kind="receipt",
            evidence={"merchant": "TEST SHOP INC", "date": "09.10.2026 11:20", "amount": "12.50",
                      "operation": "Покупка: подписка"},
        )),
    ),
    Doc(
        key="no_currency_png", filename="e2e_no_currency.png", fmt="png", marker="ЭРЕБУС",
        lines=(
            "ОПЛАТА УСЛУГ ЖКХ",
            "Получатель: ЖКХ ТЕСТ",
            "Дата: 12.10.2026 09:00",
            "Итого: 19.90",
            "Метка: ЭРЕБУС",
        ),
        answer=answer(item(
            merchant_name="ЖКХ ТЕСТ", purchase_date="2026-10-12", purchase_time="09:00", currency=None,
            total_amount="19.90", operation_type="expense", category="utilities", document_kind="payment",
            payment_status="paid",
            evidence={"merchant": "ЖКХ ТЕСТ", "date": "12.10.2026 09:00", "amount": "19.90",
                      "operation": "ОПЛАТА УСЛУГ ЖКХ"},
        )),
    ),
    Doc(
        key="no_amount_png", filename="e2e_no_amount.png", fmt="png", marker="ФОБОС",
        lines=(
            "ОПЛАТА ПАРКОВКИ",
            "Получатель: ТЕСТ ПАРКОВКА",
            "Дата: 13.10.2026 08:30",
            "Сумма: не видна на снимке",
            "Комиссия: 1.00 BYN",
            "Метка: ФОБОС",
        ),
        answer=answer(item(
            merchant_name="ТЕСТ ПАРКОВКА", purchase_date="2026-10-13", purchase_time="08:30", currency="BYN",
            total_amount=None, operation_type="expense", category="transport", document_kind="payment",
            payment_status="paid", fee_amount="1.00",
            evidence={"merchant": "ТЕСТ ПАРКОВКА", "date": "13.10.2026 08:30", "operation": "ОПЛАТА ПАРКОВКИ"},
        )),
    ),
)

BOUNDARY_MARKERS = {"0930": "СТЕКЛЯНКА", "0101": "ЮПИТЕР", "1016": "НЕПТУН", "1017": "КСЕНОН"}

# Граничные даты периода 01.10–16.10.2026 (включительно) и операции вне него.
BOUNDARY_DOCS: tuple[Doc, ...] = tuple(
    Doc(
        key=f"boundary_{key}", filename=f"e2e_boundary_{key}.png", fmt="png", marker=f"{BOUNDARY_MARKERS[key]}",
        lines=("ЧЕК ОПЛАТЫ", "Получатель: ТЕСТ ГРАНИЦА", f"Дата: {day}.{month}.2026 12:00",
               f"Покупка: тестовая {key}", f"Итого: {amount} BYN", f"Метка: {BOUNDARY_MARKERS[key]}"),
        answer=answer(item(
            merchant_name="ТЕСТ ГРАНИЦА", purchase_date=f"2026-{month}-{day}", purchase_time="12:00", currency="BYN",
            total_amount=amount, operation_type="expense", category="other", document_kind="receipt",
            evidence={"merchant": "ТЕСТ ГРАНИЦА", "date": f"{day}.{month}.2026 12:00", "amount": f"{amount}",
                      "operation": "ЧЕК ОПЛАТЫ"},
        )),
    )
    for key, day, month, amount in (
        ("0930", "30", "09", "11.11"),
        ("0101", "01", "10", "22.22"),
        ("1016", "16", "10", "33.33"),
        ("1017", "17", "10", "44.44"),
    )
) + (
    Doc(
        key="boundary_refund", filename="e2e_boundary_refund.png", fmt="png", marker="ПЛУТОНИЙ",
        lines=("ВОЗВРАТ ПЛАТЕЖА", "Получатель: ТЕСТ ГРАНИЦА", "Дата: 02.10.2026 12:00", "Возврат: 5.00 BYN",
               "Итого: 5.00 BYN", "Метка: ПЛУТОНИЙ"),
        answer=answer(item(
            merchant_name="ТЕСТ ГРАНИЦА", purchase_date="2026-10-02", purchase_time="12:00", currency="BYN",
            total_amount="5.00", operation_type="refund", category="other", document_kind="receipt",
            evidence={"merchant": "ТЕСТ ГРАНИЦА", "date": "02.10.2026 12:00", "amount": "5.00",
                      "operation": "ВОЗВРАТ ПЛАТЕЖА"},
        )),
    ),
)

DOC_BY_KEY: dict[str, Doc] = {doc.key: doc for doc in DOCS + BOUNDARY_DOCS}

# Эталон: что приложение должно сохранить. Суммы в минорных единицах (копейки/центы).
EXPECTED: dict[str, dict[str, Any]] = {
    "loan_png": {"date": "2026-10-05", "merchant": "ЗАО ТЕСТ КРЕДИТ", "amount_minor": 248800, "currency": "BYN",
                 "purpose": "Погашение кредита частичное", "category": "loan", "operation": "expense",
                 "erip": "700112233", "outflow_minor": 248800},
    "service_pdf": {"date": "2026-10-08", "merchant": "ОАО ТЕСТ СВЯЗЬ", "amount_minor": 5700, "currency": "BYN",
                    "purpose": "Интернет за сентябрь 2026", "category": "services", "operation": "expense",
                    "erip": "700445566", "fee_minor": 200, "outflow_minor": 5700},
    "store_scan": {"date": "2026-10-11", "merchant": "ТЕСТМАРКЕТ", "amount_minor": 3140, "currency": "BYN",
                   "category": "food", "operation": "expense"},
    "invoice_pdf": {"date": "2026-10-03", "merchant": "ООО ТЕСТ ЭНЕРГО", "amount_minor": 30000, "currency": "BYN",
                    "category": "utilities", "operation": "expense", "payment_status": "unpaid"},
    "refund_png": {"date": "2026-10-10", "amount_minor": 2500, "currency": "USD", "operation": "refund"},
    "usd_png": {"date": "2026-10-09", "amount_minor": 1250, "currency": "USD", "operation": "expense"},
    "no_currency_png": {"date": "2026-10-12", "amount_minor": 1990, "currency": None},
    "no_amount_png": {"date": "2026-10-13", "amount_minor": None, "currency": "BYN"},
    "loan_second_png": {"date": "2026-10-05", "erip": "700998877", "amount_minor": 248800, "currency": "BYN"},
}


def render_png(lines: tuple[str, ...]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path()), 40)
    line_height = 64
    image = Image.new("RGB", (1200, 80 + line_height * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 40 + index * line_height), line, font=font, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_text_pdf(lines: tuple[str, ...]) -> bytes:
    """PDF с настоящим текстовым слоем (шрифт встроен, извлечение текста без OCR)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if "E2EArial" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("E2EArial", str(font_path())))
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)
    pdf.setFont("E2EArial", 14)
    y = 780
    for line in lines:
        pdf.drawString(60, y, line)
        y -= 28
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def render_scan_pdf(lines: tuple[str, ...]) -> bytes:
    """PDF-скан: только изображение страницы, текстового слоя нет. Текст извлекается OCR."""
    from PIL import Image

    image = Image.open(io.BytesIO(render_png(lines))).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=200.0)
    return buffer.getvalue()


def render(doc: Doc) -> bytes:
    if doc.fmt == "png":
        return render_png(doc.lines)
    if doc.fmt == "pdf-text":
        return render_text_pdf(doc.lines)
    return render_scan_pdf(doc.lines)


def write_all(directory: Path) -> dict[str, Path]:
    """Записывает все фикстуры в каталог запуска. Возвращает путь по ключу документа."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for doc in DOCS + BOUNDARY_DOCS:
        path = directory / doc.filename
        path.write_bytes(render(doc))
        paths[doc.key] = path
    return paths


def llm_answers() -> dict[str, dict[str, Any]]:
    """Ответы подставного LLM по маркеру документа."""
    return {doc.marker: doc.answer for doc in DOCS + BOUNDARY_DOCS}
