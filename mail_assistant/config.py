from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


# Отправка чеков в облачный LLM. Основание: отметка пользователя LLM_EMAIL_USE_CONFIRMED=true
# и его подтверждение допустимости сценария. Письменного подтверждения провайдера в проекте нет,
# и это отражено в отчётах. Переключить на False, чтобы полностью отключить облако для чеков.
OPENCODE_GO_RECEIPTS_CONFIRMED = True
RECEIPT_RETRY_MAX_TOKENS = 4096


class AppError(Exception):
    """Ошибка, текст которой безопасно показать пользователю.

    details содержит только allowlist-метаданные (без текстов писем, рассуждений и заголовков).
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = details or {}


@dataclass(frozen=True)
class MailSettings:
    username: str
    app_password: str = field(repr=False)
    host: str
    port: int
    timeout_seconds: int
    max_message_bytes: int

    def require_credentials(self) -> None:
        if not self.username or not self.app_password:
            raise AppError("Не заданы MAIL_USERNAME или MAIL_APP_PASSWORD. Заполните локальный .env.")


@dataclass(frozen=True)
class LLMSettings:
    api_key: str = field(repr=False)
    api_url: str
    model: str
    models_url: str
    timeout_seconds: int
    max_output_tokens: int
    max_input_chars: int
    email_use_confirmed: bool

    def require_api_key(self) -> None:
        if not self.api_key:
            raise AppError("Не задан LLM_API_KEY. Добавьте новый ключ в локальный .env после ротации.")


def is_allowed_llm_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


def load_env_file(path: Path = ENV_FILE) -> None:
    """Загружает .env; уже заданные переменные окружения имеют приоритет."""
    if not path.is_file():
        return
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        print("Предупреждение: права на .env шире 600. Рекомендуется: chmod 600 .env", file=sys.stderr)
    load_dotenv(path, override=False)


def _get(name: str) -> str:
    return os.environ.get(name, "").strip()


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = _get(name) or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise AppError(f"{name} должен быть целым числом") from None
    if not low <= value <= high:
        raise AppError(f"{name} должен быть в диапазоне {low}..{high}")
    return value


def _bool(name: str) -> bool:
    return _get(name).lower() in TRUE_VALUES


def _url(name: str) -> str:
    value = _get(name)
    if not value:
        raise AppError(f"Не задан {name}")
    if not is_allowed_llm_url(value):
        raise AppError(f"{name} должен использовать HTTPS (HTTP допустим только для localhost)")
    return value


def load_mail_settings() -> MailSettings:
    return MailSettings(
        username=_get("MAIL_USERNAME"),
        app_password=_get("MAIL_APP_PASSWORD"),
        host=_get("IMAP_HOST") or "imap.mail.ru",
        port=_int("IMAP_PORT", 993, 1, 65535),
        timeout_seconds=_int("IMAP_TIMEOUT_SECONDS", 20, 1, 120),
        max_message_bytes=_int("MAX_MESSAGE_BYTES", 2097152, 1024, 26214400),
    )


def load_llm_settings(model: str | None = None) -> LLMSettings:
    """Приоритет модели: --model, затем LLM_MODEL."""
    chosen = (model or "").strip() or _get("LLM_MODEL")
    if not chosen:
        raise AppError("Не выбрана модель: укажите --model или задайте LLM_MODEL в .env")
    if not MODEL_NAME_RE.fullmatch(chosen):
        raise AppError("Недопустимое имя модели")
    return LLMSettings(
        api_key=_get("LLM_API_KEY"),
        api_url=_url("LLM_API_URL"),
        model=chosen,
        models_url=_url("LLM_MODELS_URL"),
        timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 60, 1, 300),
        max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 800, 1, 4096),
        max_input_chars=_int("LLM_MAX_INPUT_CHARS", 8000, 500, 50000),
        email_use_confirmed=_bool("LLM_EMAIL_USE_CONFIRMED"),
    )


@dataclass(frozen=True)
class ReceiptSettings:
    db_path: Path
    max_bytes: int
    max_pages: int
    max_pixels: int
    render_dpi: int
    ocr_timeout_seconds: int
    ocr_languages: str
    min_page_text_chars: int
    store_originals: bool
    originals_dir: Path


def _path_setting(name: str, default: str) -> Path:
    raw = _get(name) or default
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_receipt_settings() -> ReceiptSettings:
    languages = _get("RECEIPT_OCR_LANGS") or "rus+eng"
    if not re.fullmatch(r"[a-z]{3}(\+[a-z]{3}){0,3}", languages):
        raise AppError("RECEIPT_OCR_LANGS должен быть вида rus+eng")
    return ReceiptSettings(
        db_path=_path_setting("RECEIPTS_DB_PATH", "data/receipts.sqlite3"),
        max_bytes=_int("RECEIPT_MAX_BYTES", 10 * 1024 * 1024, 1024, 50 * 1024 * 1024),
        max_pages=_int("RECEIPT_MAX_PAGES", 20, 1, 100),
        max_pixels=_int("RECEIPT_MAX_PIXELS", 30_000_000, 1_000_000, 100_000_000),
        render_dpi=_int("RECEIPT_RENDER_DPI", 200, 72, 300),
        ocr_timeout_seconds=_int("RECEIPT_OCR_TIMEOUT_SECONDS", 60, 5, 600),
        ocr_languages=languages,
        min_page_text_chars=_int("RECEIPT_MIN_PAGE_TEXT_CHARS", 40, 1, 1000),
        store_originals=_bool("RECEIPT_STORE_ORIGINALS"),
        originals_dir=_path_setting("RECEIPT_ORIGINALS_DIR", "data/originals"),
    )
