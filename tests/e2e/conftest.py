"""Фикстуры сквозных тестов: изолированный запуск приложения и подставной LLM на localhost."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

import netguard
from fake_llm import FakeLLM
from harness import APP_URL, AppServer

_REAL_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection


def _loopback_connect(self: socket.socket, address: object) -> None:
    netguard.check_address(self, address)
    return _REAL_CONNECT(self, address)


@pytest.fixture(autouse=True)
def _loopback_only_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Корневой conftest запрещает любые сокеты. Здесь разрешён только localhost: проверка готовности приложения и подставной LLM."""
    monkeypatch.setattr(socket.socket, "connect", _loopback_connect)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


@pytest.fixture
def llm_answers() -> dict[str, Any]:
    """Ответы подставного LLM по маркеру документа. Сценарии переопределяют эту фикстуру."""
    return {}


@pytest.fixture
def fake_llm(llm_answers: dict[str, Any]) -> Iterator[FakeLLM]:
    llm = FakeLLM(llm_answers)
    llm.start()
    try:
        yield llm
    finally:
        llm.stop()


@pytest.fixture
def app_server(tmp_path, fake_llm: FakeLLM) -> Iterator[AppServer]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    server = AppServer(run_dir, fake_llm.base_url)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def base_url() -> str:
    """pytest-playwright: адрес тестового интерфейса (не рабочий порт 8501)."""
    return APP_URL
