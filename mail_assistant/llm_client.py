from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from mail_assistant.config import AppError, LLMSettings, is_allowed_llm_url

USER_AGENT = "mailru-ai-assistant/0.1"
MAX_RESPONSE_BYTES = 1_000_000
MAX_META_TEXT = 100
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,60}")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_STATUS_MESSAGES = {
    400: "Провайдер отклонил запрос (HTTP 400): проверьте модель и параметры",
    401: "Ключ LLM_API_KEY не принят провайдером (HTTP 401)",
    403: "Доступ запрещён провайдером (HTTP 403): проверьте ключ и условия использования",
    404: "Endpoint или модель не найдены (HTTP 404): проверьте LLM_API_URL и модель",
    429: "Превышен лимит запросов или квота (HTTP 429)",
}


class LLMError(AppError):
    """Ошибка LLM-клиента; текст не содержит ключей и сырых ответов провайдера.

    uncertain=True: запрос мог быть выполнен и оплачен (таймаут, сеть, 5xx), поэтому не повторяется автоматически.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None, *, uncertain: bool = False) -> None:
        super().__init__(message, details)
        self.uncertain = uncertain


@dataclass(frozen=True)
class ChatResult:
    content: str
    reported_model: str | None
    usage: dict[str, int] | None
    finish_reason: str | None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _raise_for_status(code: int) -> None:
    if 200 <= code < 300:
        return
    if 300 <= code < 400:
        raise LLMError("Провайдер вернул редирект; редиректы не выполняются автоматически")
    if code in _STATUS_MESSAGES:
        raise LLMError(_STATUS_MESSAGES[code])
    if code >= 500:
        raise LLMError(
            f"Ошибка на стороне провайдера (HTTP {code})", {"error_kind": "http_5xx", "http_status": code}, uncertain=True
        )
    raise LLMError(f"Неожиданный HTTP-статус {code}")


def _token(value: Any) -> str | None:
    """Короткий идентификатор из allowlist-символов; иначе только признак."""
    if value is None:
        return None
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value):
        return value
    return "invalid"


def _short_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value[:MAX_META_TEXT]
    return None


def _usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    result: dict[str, int] = {}
    for key in _USAGE_KEYS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    details = raw.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
        if isinstance(reasoning, int) and not isinstance(reasoning, bool):
            result["reasoning_tokens"] = reasoning
    return result or None


def _present(value: Any) -> bool:
    """Поле присутствует и не пустое; содержимое не возвращается."""
    if isinstance(value, (str, list, dict)):
        return bool(value)
    return value is not None and value is not False


def _content_kind(message: dict[str, Any]) -> str:
    if "content" not in message:
        return "missing"
    content = message["content"]
    if content is None:
        return "null"
    if isinstance(content, str):
        return "string" if content.strip() else "empty"
    return type(content).__name__


def describe_response(payload: Any) -> dict[str, Any]:
    """Безопасные метаданные ответа: allowlist полей, без текстов, рассуждений и заголовков."""
    info: dict[str, Any] = {"payload": type(payload).__name__}
    if not isinstance(payload, dict):
        return info
    model = _short_text(payload.get("model"))
    if model:
        info["model"] = model
    usage = _usage(payload.get("usage"))
    if usage:
        info["usage"] = usage
    provider_error = payload.get("error")
    if provider_error is not None:
        error_info: dict[str, Any] = {}
        if isinstance(provider_error, dict):
            error_info["type"] = _token(provider_error.get("type"))
            error_info["code"] = _token(provider_error.get("code"))
        info["provider_error"] = {k: v for k, v in error_info.items() if v is not None} or {"present": True}
    choices = payload.get("choices")
    if isinstance(choices, list):
        info["choices_count"] = len(choices)
    elif "choices" in payload:
        info["choices"] = type(choices).__name__
    choice = choices[0] if isinstance(choices, list) and choices else None
    if isinstance(choice, dict):
        finish = _token(choice.get("finish_reason"))
        if finish:
            info["finish_reason"] = finish
        message = choice.get("message")
        if isinstance(message, dict):
            info["content"] = _content_kind(message)
            info["reasoning"] = _present(message.get("reasoning")) or _present(message.get("reasoning_content"))
            info["tool_calls"] = _present(message.get("tool_calls"))
            info["refusal"] = _present(message.get("refusal"))
        elif "message" in choice:
            info["message"] = type(message).__name__
    return info


class LLMClient:
    """OpenAI-совместимый клиент. Один экземпляр = один логический сеанс с одним session ID.

    Повторов запросов нет: POST оплачивается, поэтому ошибки возвращаются сразу.
    """

    def __init__(self, settings: LLMSettings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._session_id = str(uuid.uuid4())
        self._http = httpx.Client(
            timeout=float(settings.timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def list_models(self) -> list[str]:
        payload = self._request_json("GET", self._settings.models_url)
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise LLMError("Ответ со списком моделей имеет неожиданную структуру")
        ids: list[str] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                ids.append(item["id"].strip())
        return ids

    def chat(self, messages: list[dict[str, str]], model: str, max_tokens: int | None = None) -> ChatResult:
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens or self._settings.max_output_tokens,
        }
        payload = self._request_json("POST", self._settings.api_url, json=body)
        details = describe_response(payload)
        if not isinstance(payload, dict):
            raise LLMError("Ответ LLM имеет неожиданную структуру", details)
        if payload.get("error") is not None:
            raise LLMError("Провайдер вернул ошибку внутри ответа HTTP 200", details)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("В ответе LLM нет choices или список пуст", details)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LLMError("choices[0] имеет неожиданный тип", details)
        message = choice.get("message")
        if message is None:
            raise LLMError("В ответе LLM отсутствует message", details)
        if not isinstance(message, dict):
            raise LLMError("message имеет неожиданный тип", details)
        if _present(message.get("refusal")):
            raise LLMError("Модель отказалась отвечать (refusal); текст не используется", details)
        if _present(message.get("tool_calls")):
            raise LLMError("Модель вернула tool_calls вместо текста; инструменты в этом сценарии не используются", details)
        finish = choice.get("finish_reason")
        if finish == "length":
            raise LLMError(
                "Генерация остановлена по лимиту (finish_reason=length): ответ неполный; "
                "автоматического повтора нет",
                details,
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMError("content отсутствует, равен null или имеет неподдерживаемый тип", details)
        if not content.strip():
            raise LLMError(
                "content пуст; reasoning не используется как результат анализа",
                details,
            )
        return ChatResult(
            content=content,
            reported_model=details.get("model"),
            usage=_usage(payload.get("usage")),
            finish_reason=finish if isinstance(finish, str) else None,
            diagnostics=details,
        )

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        self._settings.require_api_key()
        if not is_allowed_llm_url(url):
            raise LLMError("LLM endpoint должен использовать HTTPS")
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-opencode-session": self._session_id,
        }
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise LLMError("Таймаут запроса к LLM", {"error_kind": "timeout"}, uncertain=True) from None
        except httpx.HTTPError:
            raise LLMError("Сетевая ошибка при обращении к LLM", {"error_kind": "network"}, uncertain=True) from None
        _raise_for_status(response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise LLMError("Ответ LLM слишком большой")
        try:
            return response.json()
        except ValueError:
            raise LLMError("Ответ HTTP не является корректным JSON", {"payload": "invalid_json"}) from None
