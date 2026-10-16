from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from mail_assistant.config import LLMSettings, ReceiptSettings
from mail_assistant.llm_client import LLMClient
from mail_assistant.ocr import OcrError
from mail_assistant.receipts import AiOptions, LLMBudget
from mail_assistant.storage import Store

RECEIPT_TEXT_LINES = [
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


class FakeOcr:
    def __init__(self, text: str = "МАГАЗИН ТЕСТ\nИТОГО 500,00 RUB\n01.10.2026 12:00") -> None:
        self.text = text
        self.calls = 0

    def ensure_available(self) -> None:
        return None

    def image_to_text(self, image: Any, psm: int = 4) -> str:
        self.calls += 1
        return self.text


class UnavailableOcr:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_available(self) -> None:
        raise OcrError("Tesseract не найден. Установите его (см. README, раздел OCR).", "OCR_UNAVAILABLE")

    def image_to_text(self, image: Any, psm: int = 4) -> str:
        self.calls += 1
        self.ensure_available()
        return ""


def receipt_settings(tmp_path: Path, **overrides: Any) -> ReceiptSettings:
    base = ReceiptSettings(
        db_path=tmp_path / "receipts.sqlite3",
        max_bytes=5 * 1024 * 1024,
        max_pages=5,
        max_pixels=5_000_000,
        render_dpi=72,
        ocr_timeout_seconds=5,
        ocr_languages="rus+eng",
        min_page_text_chars=20,
        store_originals=False,
        originals_dir=tmp_path / "originals",
    )
    return replace(base, **overrides)


def llm_settings(**overrides: Any) -> LLMSettings:
    base = LLMSettings(
        api_key="test-key-123",
        api_url="https://llm.example.test/v1/chat/completions",
        model="deepseek-v4.1-flash",
        models_url="https://llm.example.test/v1/models",
        timeout_seconds=5,
        max_output_tokens=800,
        max_input_chars=8000,
        email_use_confirmed=True,
    )
    return replace(base, **overrides)


def completion(content: Any, *, finish: str = "stop", usage: dict | None = None) -> dict:
    body: dict[str, Any] = {
        "model": "deepseek-v4.1-flash",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def receipt_json(**fields: Any) -> str:
    item = {
        "merchant_name": "MAGAZIN TEST LLC",
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
            "merchant": "MAGAZIN TEST LLC",
            "date": "05.10.2026 14:32",
            "amount": "ITOGO 900.00 RUB",
            "operation": "SALE 1000.00 RUB",
        },
        "warnings": [],
    }
    item.update(fields)
    return json.dumps({"receipts": [item]}, ensure_ascii=False)


class FakeLLM:
    """Mock-транспорт: отдаёт заранее заданный ответ и считает вызовы."""

    def __init__(self, responder: Any) -> None:
        self.calls: list[httpx.Request] = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        result = self._responder(request)
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    def factory(self, llm: LLMSettings) -> LLMClient:
        return LLMClient(llm, transport=httpx.MockTransport(self.handler))


def ai_options(fake: FakeLLM | None, *, budget: int = 5, llm: LLMSettings | None = None, allow: bool = True) -> AiOptions:
    settings = llm or llm_settings()
    factory = fake.factory if fake is not None else LLMClient
    return AiOptions(
        allow_cloud=allow,
        llm_settings=settings if allow else None,
        budget=LLMBudget(budget),
        client_factory=factory,
    )


def open_store(tmp_path: Path, settings: ReceiptSettings) -> Store:
    return Store(settings.db_path)
