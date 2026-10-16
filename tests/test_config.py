from __future__ import annotations

import os

import pytest

from mail_assistant.config import (
    AppError,
    MailSettings,
    is_allowed_llm_url,
    load_env_file,
    load_llm_settings,
    load_mail_settings,
)


def test_env_variables_take_priority_over_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("IMAP_HOST=from-file.example\nMAIL_USERNAME=file@mail.ru\n", encoding="utf-8")
    monkeypatch.setenv("IMAP_HOST", "from-env.example")

    load_env_file(env_file)

    assert os.environ["IMAP_HOST"] == "from-env.example"
    assert load_mail_settings().username == "file@mail.ru"


def test_mail_check_settings_do_not_need_llm(monkeypatch):
    monkeypatch.setenv("MAIL_USERNAME", "user@mail.ru")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "app-pass")

    settings = load_mail_settings()

    assert settings.host == "imap.mail.ru"
    assert settings.port == 993
    assert settings.max_message_bytes == 2097152


def test_llm_settings_do_not_need_mail(llm_env):
    settings = load_llm_settings()

    assert settings.model == "test-model"
    assert settings.email_use_confirmed is True


def test_cli_model_has_priority_over_llm_model(llm_env):
    assert load_llm_settings("cli-model").model == "cli-model"
    assert load_llm_settings().model == "test-model"


def test_model_is_required(monkeypatch, llm_env):
    monkeypatch.delenv("LLM_MODEL")
    with pytest.raises(AppError):
        load_llm_settings()


def test_plain_http_is_rejected_for_remote_endpoint(monkeypatch, llm_env):
    monkeypatch.setenv("LLM_API_URL", "http://llm.example.test/v1/chat/completions")
    with pytest.raises(AppError, match="HTTPS"):
        load_llm_settings()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://opencode.ai/inference/go/openai/v1/chat/completions", True),
        ("http://localhost:8080/v1/chat/completions", True),
        ("http://example.com/v1/chat/completions", False),
        ("https://user:pass@example.com/v1", False),
        ("ftp://example.com/v1", False),
        ("", False),
    ],
)
def test_llm_url_policy(url, allowed):
    assert is_allowed_llm_url(url) is allowed


def test_password_is_not_shown_in_repr():
    settings = MailSettings(
        username="user@mail.ru",
        app_password="super-secret-value",
        host="imap.mail.ru",
        port=993,
        timeout_seconds=5,
        max_message_bytes=1000,
    )
    assert "super-secret-value" not in repr(settings)


def test_invalid_integer_setting_is_reported(monkeypatch):
    monkeypatch.setenv("IMAP_PORT", "abc")
    with pytest.raises(AppError, match="IMAP_PORT"):
        load_mail_settings()


def test_llm_stage_limit_defaults_to_150_and_is_configurable(monkeypatch):
    from mail_assistant.config import llm_stage_limit

    monkeypatch.delenv("LLM_STAGE_LIMIT", raising=False)
    assert llm_stage_limit() == 150
    monkeypatch.setenv("LLM_STAGE_LIMIT", "120")
    assert llm_stage_limit() == 120
