from __future__ import annotations

import socket
from typing import Any

import pytest

from helpers import LLM_API_URL, LLM_KEY, LLM_MODELS_URL, PASSWORD, USER, FakeConnection, FakeServer
from mail_assistant import imap_client
from mail_assistant.config import LLMSettings, MailSettings

KNOWN_ENV = [
    "MAIL_USERNAME", "MAIL_APP_PASSWORD", "IMAP_HOST", "IMAP_PORT", "IMAP_TIMEOUT_SECONDS",
    "MAX_MESSAGE_BYTES", "LLM_API_KEY", "LLM_API_URL", "LLM_MODEL", "LLM_MODELS_URL",
    "LLM_TIMEOUT_SECONDS", "LLM_MAX_OUTPUT_TOKENS", "LLM_MAX_INPUT_CHARS", "LLM_EMAIL_USE_CONFIRMED",
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv + delenv: переменные, которые загрузит тест, будут удалены после него.
    for name in KNOWN_ENV:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setattr("mail_assistant.__main__.load_env_file", lambda *args, **kwargs: None)

    def _blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("В тестах запрещены сетевые подключения")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture(autouse=True)
def _provider_confirmed_for_mock_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Моки LLM проверяют логику, а не провайдера. Гейт проверяется отдельным тестом."""
    monkeypatch.setattr("mail_assistant.receipts.OPENCODE_GO_RECEIPTS_CONFIRMED", True)


@pytest.fixture
def mail_settings() -> MailSettings:
    return MailSettings(
        username=USER,
        app_password=PASSWORD,
        host="imap.mail.ru",
        port=993,
        timeout_seconds=5,
        max_message_bytes=200_000,
    )


@pytest.fixture
def mail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_USERNAME", USER)
    monkeypatch.setenv("MAIL_APP_PASSWORD", PASSWORD)


@pytest.fixture
def llm_settings() -> LLMSettings:
    return LLMSettings(
        api_key=LLM_KEY,
        api_url=LLM_API_URL,
        model="test-model",
        models_url=LLM_MODELS_URL,
        timeout_seconds=5,
        max_output_tokens=100,
        max_input_chars=1000,
        email_use_confirmed=False,
    )


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("LLM_API_URL", LLM_API_URL)
    monkeypatch.setenv("LLM_MODELS_URL", LLM_MODELS_URL)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_EMAIL_USE_CONFIRMED", "true")


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    server = FakeServer()

    def factory(host: str, port: int, ssl_context: Any = None, timeout: float | None = None) -> FakeConnection:
        server.connections.append({"host": host, "port": port, "ssl_context": ssl_context, "timeout": timeout})
        return FakeConnection(server)

    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", factory)
    return server
