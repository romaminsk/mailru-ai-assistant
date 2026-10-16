from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["work", "finance", "personal", "newsletter", "other"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Task(_Strict):
    description: str = Field(min_length=1, max_length=500)
    deadline: str | None = Field(default=None, max_length=100)
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator("deadline", mode="before")
    @classmethod
    def _empty_deadline_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class EmailAnalysis(_Strict):
    category: Category
    summary: str = Field(min_length=1, max_length=1000)
    needs_reply: bool
    tasks: list[Task] = Field(default_factory=list, max_length=20)
