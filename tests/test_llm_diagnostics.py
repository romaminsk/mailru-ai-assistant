from __future__ import annotations

import json

import httpx
import pytest

import mail_assistant.__main__ as cli
from helpers import LLM_KEY, build_email
from mail_assistant.analysis import AnalysisError, parse_analysis
from mail_assistant.config import AppError
from mail_assistant.llm_client import LLMClient, LLMError, describe_response
from mail_assistant.__main__ import main

SECRET_TEXTS = ["SECRET-REASONING", "SECRET-REFUSAL", "SECRET-TOOL-ARGS", "SECRET-PROVIDER-MSG", "SECRET-EMAIL"]


def _client(settings, payload=None, *, status=200, raw: bytes | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=payload)

    return LLMClient(settings, transport=httpx.MockTransport(handler))


def _chat(settings, payload=None, **kwargs):
    with _client(settings, payload, **kwargs) as client:
        return client.chat([{"role": "user", "content": "x"}], "test-model")


def _assert_no_secret_text(*values) -> None:
    joined = " ".join(str(value) for value in values)
    for secret in SECRET_TEXTS + [LLM_KEY]:
        assert secret not in joined


def _completion(message, *, finish="stop", model="test-model-actual", usage=None, choices=None):
    if choices is None:
        choices = [{"message": message, "finish_reason": finish}]
    body = {"model": model, "choices": choices}
    if usage is not None:
        body["usage"] = usage
    return body


def test_successful_response_returns_content_and_metadata(llm_settings):
    result = _chat(
        llm_settings,
        _completion(
            {"role": "assistant", "content": "ок"},
            usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        ),
    )

    assert result.content == "ок"
    assert result.reported_model == "test-model-actual"
    assert result.diagnostics["content"] == "string"
    assert result.diagnostics["reasoning"] is False
    assert result.diagnostics["usage"]["total_tokens"] == 13


def test_content_null_with_length_is_truncation_not_success(llm_settings):
    payload = _completion(
        {"role": "assistant", "content": None},
        finish="length",
        usage={"prompt_tokens": 50, "completion_tokens": 800, "total_tokens": 850},
    )

    with pytest.raises(LLMError, match="finish_reason=length") as excinfo:
        _chat(llm_settings, payload)

    details = excinfo.value.details
    assert details["finish_reason"] == "length"
    assert details["content"] == "null"
    assert details["model"] == "test-model-actual"
    assert details["usage"]["completion_tokens"] == 800
    assert "автоматического повтора нет" in str(excinfo.value)


def test_truncation_is_reported_even_when_partial_text_present(llm_settings):
    payload = _completion({"role": "assistant", "content": "обрезанный текст"}, finish="length")

    with pytest.raises(LLMError, match="length"):
        _chat(llm_settings, payload)


def test_reasoning_without_content_is_not_used_as_result(llm_settings):
    payload = _completion({"role": "assistant", "content": "", "reasoning_content": "SECRET-REASONING"})

    with pytest.raises(LLMError, match="reasoning не используется") as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["reasoning"] is True
    assert excinfo.value.details["content"] == "empty"
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_reasoning_field_alone_is_detected_without_content(llm_settings):
    payload = _completion({"reasoning": "SECRET-REASONING"}, finish="stop")

    with pytest.raises(LLMError) as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["reasoning"] is True
    assert excinfo.value.details["content"] == "missing"
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_empty_choices_are_reported(llm_settings):
    with pytest.raises(LLMError, match="choices") as excinfo:
        _chat(llm_settings, {"model": "test-model-actual", "choices": []})

    assert excinfo.value.details["choices_count"] == 0
    assert excinfo.value.details["model"] == "test-model-actual"


def test_missing_message_is_reported(llm_settings):
    payload = {"model": "test-model-actual", "choices": [{"finish_reason": "stop"}]}

    with pytest.raises(LLMError, match="message") as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["choices_count"] == 1


def test_refusal_is_reported_without_refusal_text(llm_settings):
    payload = _completion({"role": "assistant", "content": None, "refusal": "SECRET-REFUSAL"})

    with pytest.raises(LLMError, match="отказалась") as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["refusal"] is True
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_tool_calls_instead_of_text_are_rejected(llm_settings):
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "1", "type": "function", "function": {"name": "send", "arguments": "SECRET-TOOL-ARGS"}}],
    }

    with pytest.raises(LLMError, match="tool_calls") as excinfo:
        _chat(llm_settings, _completion(message, finish="tool_calls"))

    assert excinfo.value.details["tool_calls"] is True
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_provider_error_inside_http_200_is_reported_by_allowlisted_fields(llm_settings):
    payload = {
        "error": {"type": "rate_limit_error", "code": "quota-42", "message": "SECRET-PROVIDER-MSG"},
    }

    with pytest.raises(LLMError, match="внутри ответа HTTP 200") as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["provider_error"] == {"type": "rate_limit_error", "code": "quota-42"}
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_invalid_json_in_http_response_is_reported(llm_settings):
    with pytest.raises(LLMError, match="JSON") as excinfo:
        _chat(llm_settings, raw=b"<html>SECRET-EMAIL</html>")

    assert excinfo.value.details == {"payload": "invalid_json"}
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_unsupported_content_type_is_reported_by_type_name(llm_settings):
    payload = _completion({"role": "assistant", "content": [{"type": "text", "text": "SECRET-EMAIL"}]})

    with pytest.raises(LLMError, match="неподдерживаемый") as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["content"] == "list"
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_usage_keeps_only_known_numeric_counters(llm_settings):
    usage = {
        "prompt_tokens": 5,
        "completion_tokens": 9,
        "total_tokens": 14,
        "secret_debug": "SECRET-EMAIL",
        "cost": True,
        "completion_tokens_details": {"reasoning_tokens": 7, "note": "SECRET-REASONING"},
    }
    payload = _completion({"role": "assistant", "content": None}, finish="length", usage=usage)

    with pytest.raises(LLMError) as excinfo:
        _chat(llm_settings, payload)

    assert excinfo.value.details["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 9,
        "total_tokens": 14,
        "reasoning_tokens": 7,
    }
    _assert_no_secret_text(excinfo.value.details)


def test_length_without_reasoning_counter_does_not_claim_reasoning_budget(llm_settings):
    payload = _completion({"role": "assistant", "content": None}, finish="length", usage={"completion_tokens": 800})

    with pytest.raises(LLMError) as excinfo:
        _chat(llm_settings, payload)

    message = str(excinfo.value)
    assert "reasoning" not in message
    assert "рассужд" not in message
    assert "reasoning_tokens" not in excinfo.value.details["usage"]


def test_model_is_truncated_and_invalid_tokens_are_masked():
    info = describe_response(
        {
            "model": "m" * 500,
            "choices": [{"finish_reason": "stop with spaces and SECRET", "message": {"content": "x"}}],
            "error": {"type": "bad type\n", "code": 12},
        }
    )

    assert len(info["model"]) == 100
    assert info["finish_reason"] == "invalid"
    assert info["provider_error"] == {"type": "invalid", "code": "invalid"}


def test_describe_response_never_contains_message_text():
    info = describe_response(
        {
            "model": "test-model-actual",
            "choices": [{"finish_reason": "stop", "message": {"content": "SECRET-EMAIL", "refusal": "SECRET-REFUSAL"}}],
        }
    )

    _assert_no_secret_text(info)
    assert info["refusal"] is True


def test_schema_error_contains_paths_and_no_values():
    content = json.dumps(
        {"category": "SECRET-EMAIL-CATEGORY", "summary": "ok", "needs_reply": True, "extra_SECRET": "SECRET-REASONING"},
        ensure_ascii=False,
    )

    with pytest.raises(AnalysisError) as excinfo:
        parse_analysis(content, {"model": "test-model-actual"})

    details = excinfo.value.details
    assert details["model"] == "test-model-actual"
    locs = {item["loc"] for item in details["schema_errors"]}
    assert "category" in locs
    assert len(details["schema_errors"]) <= 5
    text = json.dumps(details, ensure_ascii=False)
    assert "SECRET-EMAIL-CATEGORY" not in text
    assert "SECRET-REASONING" not in text


def test_non_json_analysis_keeps_metadata_and_no_text():
    with pytest.raises(AnalysisError) as excinfo:
        parse_analysis("SECRET-EMAIL plain text", {"finish_reason": "stop"})

    assert excinfo.value.details == {"finish_reason": "stop"}
    _assert_no_secret_text(str(excinfo.value), excinfo.value.details)


def test_cli_prints_metadata_but_not_texts(fake_server, mail_env, llm_env, monkeypatch, capsys):
    fake_server.add(101, build_email(plain="SECRET-EMAIL body"))
    payload = _completion(
        {"role": "assistant", "content": "", "reasoning_content": "SECRET-REASONING"},
        model="test-model-actual",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(cli, "LLMClient", lambda settings: LLMClient(settings, transport=transport))

    assert main(["analyze", "--uid", "101", "--model", "test-model", "--allow-cloud"]) == 1

    captured = capsys.readouterr()
    assert "reasoning не используется" in captured.err
    assert "Метаданные ответа" in captured.err
    assert '"model": "test-model-actual"' in captured.err
    assert '"reasoning": true' in captured.err
    assert captured.out == ""
    _assert_no_secret_text(captured.err, captured.out)
    assert "SECRET-EMAIL" not in captured.err + captured.out


def test_app_error_default_details_are_empty():
    assert AppError("обычная ошибка").details == {}
