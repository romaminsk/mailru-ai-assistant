from __future__ import annotations

from typing import Any, Protocol

from mail_assistant.config import AppError

OCR_PSM = 4  # колонка текста переменной высоты: основной режим для чеков
OCR_PSM_ALT = 6  # один блок текста: запасная попытка при плохом результате


class OcrError(AppError):
    """Ошибка OCR с кодом для статуса документа."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class OcrEngine(Protocol):
    def ensure_available(self) -> None: ...

    def image_to_text(self, image: Any, psm: int = OCR_PSM) -> str: ...


class TesseractOcr:
    """OCR через системный Tesseract (pytesseract). Tesseract устанавливается отдельно."""

    def __init__(self, languages: str, timeout_seconds: int) -> None:
        self._languages = languages
        self._timeout = timeout_seconds
        self._checked = False

    def ensure_available(self) -> None:
        if self._checked:
            return
        import pytesseract

        try:
            pytesseract.get_tesseract_version()
        except Exception:
            raise OcrError(
                "Tesseract не найден. Установите его (см. README, раздел OCR) и повторите.",
                "OCR_UNAVAILABLE",
            ) from None
        try:
            installed = set(pytesseract.get_languages(config=""))
        except Exception:
            installed = set()
        missing = [lang for lang in self._languages.split("+") if lang not in installed]
        if missing:
            raise OcrError(
                f"Не установлены языки Tesseract: {', '.join(missing)}. См. README, раздел OCR.",
                "OCR_LANG_MISSING",
            )
        self._checked = True

    def image_to_text(self, image: Any, psm: int = OCR_PSM) -> str:
        import pytesseract

        self.ensure_available()
        try:
            return pytesseract.image_to_string(
                image, lang=self._languages, config=f"--psm {psm}", timeout=self._timeout
            )
        except RuntimeError as exc:
            if "timeout" in str(exc).lower():
                raise OcrError("Время OCR истекло (RECEIPT_OCR_TIMEOUT_SECONDS)", "OCR_TIMEOUT") from None
            raise OcrError("Ошибка OCR", "OCR_FAILED") from None
        except Exception:
            raise OcrError("Ошибка OCR", "OCR_FAILED") from None
