from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mail_assistant.money import SUPPORTED_CURRENCIES, parse_amount

OperationType = Literal["expense", "refund", "unknown"]
ReceiptCategory = Literal["food", "transport", "health", "household", "clothing", "electronics", "services", "entertainment", "loan", "utilities", "other"]
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?")
_TAX_RE = re.compile(r"\d{9}|\d{10}|\d{12}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _empty_strings_to_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: (None if isinstance(value, str) and not value.strip() else value) for key, value in data.items()}
        return data


class ReceiptEvidence(_Strict):
    merchant: str | None = Field(default=None, max_length=300)
    date: str | None = Field(default=None, max_length=300)
    amount: str | None = Field(default=None, max_length=300)
    operation: str | None = Field(default=None, max_length=300)


class ReceiptItem(_Strict):
    merchant_name: str | None = Field(default=None, max_length=200)
    merchant_tax_id: str | None = Field(default=None, max_length=12)
    purchase_date: str | None = Field(default=None, max_length=10)
    purchase_time: str | None = Field(default=None, max_length=8)
    currency: str | None = Field(default=None, max_length=3)
    total_amount: str | None = Field(default=None, max_length=20)
    operation_type: OperationType
    category: ReceiptCategory = "other"
    receipt_number: str | None = Field(default=None, max_length=100)
    fiscal_id: str | None = Field(default=None, max_length=100)
    document_kind: Literal["receipt", "payment", "invoice", "unknown"] = "unknown"
    payment_status: Literal["paid", "unpaid", "unknown"] = "unknown"
    evidence: ReceiptEvidence = Field(default_factory=ReceiptEvidence)
    warnings: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("purchase_date")
    @classmethod
    def _valid_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("дата должна быть в формате YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    @field_validator("purchase_time")
    @classmethod
    def _valid_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _TIME_RE.fullmatch(value):
            raise ValueError("время должно быть в формате HH:MM[:SS]")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _valid_currency(cls, value: Any) -> str | None:
        # Неподдерживаемая валюта не угадывается: поле становится null и запись уходит на проверку.
        if not isinstance(value, str):
            return None
        code = value.strip().upper()
        return code if code in SUPPORTED_CURRENCIES else None

    @field_validator("total_amount")
    @classmethod
    def _valid_amount(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_amount(value)
        return value

    @field_validator("merchant_tax_id", mode="before")
    @classmethod
    def _valid_tax(cls, value: Any) -> str | None:
        # Необязательное поле: цифры извлекаются, некорректный ИНН очищается до null, а не отбрасывает весь ответ.
        if not isinstance(value, str):
            return None
        digits = re.sub(r"\D", "", value)
        return digits if _TAX_RE.fullmatch(digits) else None


class ReceiptBatch(_Strict):
    receipts: list[ReceiptItem] = Field(default_factory=list, max_length=20)
