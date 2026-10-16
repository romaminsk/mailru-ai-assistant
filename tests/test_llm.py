from __future__ import annotations

import json
import re

import httpx
import pytest

from helpers import LLM_API_URL, LLM_KEY, LLM_MODELS_URL
from mail_assistant.config import AppError
from mail_assistant.llm_client import LLMClient, LLMError

OK_BODY = {
    "model": "test-model-actual",
    "choices": [{"message": {"role": "assistant", "content": "Ответ"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _client(settings, handler):
    return LLMClient(settings, transport=httpx.MockTransport(handler))


def test_post_goes_to_exact_api_url_with_expected_headers_and_body(llm_settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    with _client(llm_settings, handler) as client:
        result = client.chat([{"role": "user", "content": "привет"}], "test-model")

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == LLM_API_URL
    assert request.headers["Authorization"] == f"Bearer {LLM_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"] == "mailru-ai-assistant/0.1"
    body = json.loads(request.content)
    assert body == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "привет"}],
        "stream": False,
        "max_tokens": 100,
    }
    assert result.content == "Ответ"
    assert result.reported_model == "test-model-actual"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_session_id_is_stable_within_client_and_new_per_client(llm_settings):
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        return httpx.Response(200, json=OK_BODY)

    with _client(llm_settings, handler) as client:
        client.chat([{"role": "user", "content": "1"}], "test-model")
        client.chat([{"role": "user", "content": "2"}], "test-model")
    with _client(llm_settings, handler) as other:
        other.chat([{"role": "user", "content": "3"}], "test-model")

    assert sessions[0] == sessions[1]
    assert sessions[0] != sessions[2]
    assert re.fullmatch(r"[0-9a-f-]{36}", sessions[0])


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (400, "400"),
        (401, "LLM_API_KEY"),
        (403, "403"),
        (404, "404"),
        (429, "429"),
        (500, "500"),
        (503, "503"),
    ],
)
def test_http_errors_are_mapped_without_leaking_key_or_body(llm_settings, status, fragment):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"SECRET-BODY {LLM_KEY} Authorization")

    with _client(llm_settings, handler) as client:
        with pytest.raises(LLMError, match=re.escape(fragment)) as excinfo:
            client.chat([{"role": "user", "content": "x"}], "test-model")

    message = str(excinfo.value)
    assert LLM_KEY not in message
    assert "SECRET-BODY" not in message
    assert "Authorization" not in message


def test_redirect_is_not_followed(llm_settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example.test/steal"})

    with _client(llm_settings, handler) as client:
        with pytest.raises(LLMError, match="редирект"):
            client.chat([{"role": "user", "content": "x"}], "test-model")

    assert len(calls) == 1


def test_timeout_is_reported_and_not_retried(llm_settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    with _client(llm_settings, handler) as client:
        with pytest.raises(LLMError, match="Таймаут"):
            client.chat([{"role": "user", "content": "x"}], "test-model")

    assert len(calls) == 1


def test_broken_json_is_rejected(llm_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with _client(llm_settings, handler) as client:
        with pytest.raises(LLMError, match="JSON"):
            client.chat([{"role": "user", "content": "x"}], "test-model")


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"unexpected": True},
    ],
)
def test_missing_content_is_rejected(llm_settings, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _client(llm_settings, handler) as client:
        with pytest.raises(LLMError):
            client.chat([{"role": "user", "content": "x"}], "test-model")


def test_list_models_returns_exact_ids_from_response(llm_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == LLM_MODELS_URL
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}, {"name": "x"}]})

    with _client(llm_settings, handler) as client:
        assert client.list_models() == ["model-a", "model-b"]


def test_insecure_endpoint_is_refused_by_client(llm_settings):
    from dataclasses import replace

    insecure = replace(llm_settings, api_url="http://attacker.example.test/v1/chat/completions")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK_BODY)

    with _client(insecure, handler) as client:
        with pytest.raises(LLMError, match="HTTPS"):
            client.chat([{"role": "user", "content": "x"}], "test-model")

    assert calls == []


def test_missing_api_key_is_reported_before_request(llm_settings):
    from dataclasses import replace

    no_key = replace(llm_settings, api_key="")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK_BODY)

    with _client(no_key, handler) as client:
        with pytest.raises(AppError, match="LLM_API_KEY"):
            client.chat([{"role": "user", "content": "x"}], "test-model")

    assert calls == []
