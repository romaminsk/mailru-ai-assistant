from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from mail_assistant.config import AppError, ReceiptSettings
from mail_assistant.ocr import OCR_PSM, OCR_PSM_ALT, OcrEngine, OcrError

EXTRACTOR_VERSION = "extractor-2"
OCR_MIN_QUALITY = 5  # ниже этого порога текст считается плохим
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PDF_MAGIC = b"%PDF-"


class DocumentError(AppError):
    """Ошибка извлечения документа с безопасным кодом статуса."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PageText:
    number: int
    source: str  # pdf_text | ocr
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    mime_type: str
    pages: tuple[PageText, ...]
    page_count: int

    def combined_text(self) -> str:
        return "\n\n".join(
            f"--- стр. {page.number}, источник: {page.source} ---\n{page.text}" for page in self.pages
        )


def detect_kind(data: bytes) -> tuple[str, str]:
    """Тип по сигнатуре файла; расширение и имя вложения не учитываются."""
    if data.startswith(PNG_MAGIC):
        return "png", "image/png"
    if data.startswith(PDF_MAGIC):
        return "pdf", "application/pdf"
    raise DocumentError("Поддерживаются только PNG и PDF (определено по содержимому)", "UNSUPPORTED_TYPE")


def extract_document(data: bytes, settings: ReceiptSettings, ocr: OcrEngine) -> ExtractedDocument:
    if len(data) > settings.max_bytes:
        raise DocumentError("Файл больше лимита RECEIPT_MAX_BYTES", "FILE_TOO_LARGE")
    kind, mime = detect_kind(data)
    if kind == "png":
        return _extract_png(data, settings, ocr, mime)
    return _extract_pdf(data, settings, ocr, mime)


def _run_ocr(image: Any, ocr: OcrEngine, psm: int = OCR_PSM) -> str:
    try:
        return ocr.image_to_text(image, psm=psm)
    except OcrError as exc:
        raise DocumentError(str(exc), exc.code) from None


def ocr_quality(text: str) -> int:
    """Грубая оценка: число цифр и слов из 3+ букв. Используется только для выбора между попытками."""
    return len(re.findall(r"\d", text)) + len(re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text))


def prepare_image(image: Any, max_pixels: int) -> Any:
    """Поворот по EXIF, белый фон вместо прозрачности, масштаб мелкого текста, мягкий контраст.

    Бинаризация не применяется: она стирает мелкие цифры.
    """
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    gray = image.convert("L")
    width, height = gray.size
    if max(width, height) < 1600:
        factor = 2
        if width * factor * height * factor <= max_pixels:
            gray = gray.resize((width * factor, height * factor), Image.LANCZOS)
    return ImageOps.autocontrast(gray, cutoff=1)


def ocr_image_best(image: Any, ocr: OcrEngine) -> str:
    """Основной режим OCR; одна альтернативная попытка, если результат выглядит плохо."""
    primary = _run_ocr(image, ocr, OCR_PSM).strip()
    if ocr_quality(primary) >= OCR_MIN_QUALITY:
        return primary
    alternative = _run_ocr(image, ocr, OCR_PSM_ALT).strip()
    return alternative if ocr_quality(alternative) > ocr_quality(primary) else primary


def _extract_png(data: bytes, settings: ReceiptSettings, ocr: OcrEngine, mime: str) -> ExtractedDocument:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format != "PNG":
                raise DocumentError("Файл не является PNG", "UNSUPPORTED_TYPE")
            width, height = probe.size
            if width * height > settings.max_pixels:
                raise DocumentError("Изображение больше лимита RECEIPT_MAX_PIXELS", "IMAGE_TOO_LARGE")
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            prepared = prepare_image(image, settings.max_pixels)
    except DocumentError:
        raise
    except Image.DecompressionBombError:
        raise DocumentError("Изображение похоже на decompression bomb", "IMAGE_TOO_LARGE") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise DocumentError("PNG повреждён или не читается", "FILE_CORRUPT") from None
    text = ocr_image_best(prepared, ocr)
    if not text:
        raise DocumentError("OCR не нашёл текста на изображении", "NO_TEXT")
    if ocr_quality(text) < OCR_MIN_QUALITY:
        raise DocumentError("Текст распознан плохо: цифры и слова не читаются", "OCR_POOR")
    return ExtractedDocument(mime_type=mime, pages=(PageText(1, "ocr", text),), page_count=1)


def _extract_pdf(data: bytes, settings: ReceiptSettings, ocr: OcrEngine, mime: str) -> ExtractedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("PDF зашифрован паролем; расшифровка не выполняется", "PDF_ENCRYPTED")
        page_count = len(reader.pages)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("PDF повреждён или не читается", "FILE_CORRUPT") from None
    if page_count == 0:
        raise DocumentError("В PDF нет страниц", "NO_TEXT")
    if page_count > settings.max_pages:
        raise DocumentError("В PDF больше страниц, чем RECEIPT_MAX_PAGES", "TOO_MANY_PAGES")

    pages: list[PageText] = []
    pdf = None
    try:
        for index in range(page_count):
            try:
                embedded = (reader.pages[index].extract_text() or "").strip()
            except Exception:
                embedded = ""
            if len(embedded) >= settings.min_page_text_chars:
                pages.append(PageText(index + 1, "pdf_text", embedded))
                continue
            if pdf is None:
                pdf = _open_pdfium(data)
            image = _render_page(pdf, index, settings)
            pages.append(PageText(index + 1, "ocr", _run_ocr(image, ocr).strip()))
    finally:
        if pdf is not None:
            pdf.close()
    if not any(page.text for page in pages):
        raise DocumentError("Текст не найден ни встроенным слоем, ни OCR", "NO_TEXT")
    return ExtractedDocument(mime_type=mime, pages=tuple(pages), page_count=page_count)


def _open_pdfium(data: bytes) -> Any:
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(data)
    except Exception:
        raise DocumentError("PDF не открывается для рендеринга", "FILE_CORRUPT") from None


def _render_page(pdf: Any, index: int, settings: ReceiptSettings) -> Any:
    scale = settings.render_dpi / 72
    try:
        page = pdf[index]
        width_pt, height_pt = page.get_size()
        if (width_pt * scale) * (height_pt * scale) > settings.max_pixels:
            raise DocumentError("Страница больше лимита RECEIPT_MAX_PIXELS", "IMAGE_TOO_LARGE")
        return page.render(scale=scale).to_pil()
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("Не удалось отрисовать страницу PDF", "FILE_CORRUPT") from None
