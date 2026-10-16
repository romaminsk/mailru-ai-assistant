"""Интерфейс Streamlit на временной базе. Облако и почта не используются."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import mail_assistant.receipts as receipts_module
from receipt_fixtures import FakeLLM, FakeOcr, RECEIPT_TEXT_LINES, completion, llm_settings, receipt_json, receipt_settings
from synthetic_docs import make_pdf
from mail_assistant import web_services as svc
from mail_assistant.receipts import AiOptions, LLMBudget, SourceRef, process_document
from mail_assistant.storage import Store


@pytest.fixture
def web_db(tmp_path, monkeypatch):
    settings = receipt_settings(tmp_path)
    monkeypatch.setenv("RECEIPTS_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("RECEIPT_ORIGINALS_DIR", str(settings.originals_dir))
    fake = FakeLLM(lambda request: completion(receipt_json()))
    with Store(settings.db_path) as store:
        ai = AiOptions(allow_cloud=True, llm_settings=llm_settings(), budget=LLMBudget(5), client_factory=fake.factory)
        result = process_document(
            store, make_pdf([{"lines": RECEIPT_TEXT_LINES}]), source=SourceRef(part_name="web.pdf"),
            settings=settings, ocr=FakeOcr(), ai=ai,
        )
    return {"db": settings.db_path, "receipt_id": result.receipt_ids[0], "llm_calls": fake}


WEB_SCRIPT = str(Path(__file__).resolve().parents[1] / "mail_assistant" / "web.py")


def _app() -> AppTest:
    return AppTest.from_file(WEB_SCRIPT, default_timeout=120)


def test_page_renders_receipts_and_pending_report_without_actions(web_db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("перерисовка не должна обращаться к LLM или почте")

    monkeypatch.setattr(receipts_module, "LLMClient", forbidden)
    calls_before = len(web_db["llm_calls"].calls)
    at = _app().run()

    assert not at.exception
    assert len(at.dataframe) >= 2
    assert any("Предварительно" in caption.value for caption in at.caption)
    assert len(web_db["llm_calls"].calls) == calls_before


def test_confirm_button_marks_receipt_and_report_counts_it(web_db):
    at = _app().run()
    confirm = next(b for b in at.button if b.label == "Подтвердить")
    confirm.click().run()

    assert not at.exception
    with Store(web_db["db"]) as store:
        assert store.receipt(web_db["receipt_id"])["review_status"] == "confirmed"
        report = svc.report_with_pending(store, "2026-10-01", "2026-10-31")
    assert report["currencies"]["RUB"]["expense"] == "900.00"


def test_reject_button_removes_from_confirmed_totals(web_db):
    at = _app().run()
    next(b for b in at.button if b.label == "Подтвердить").click().run()
    next(b for b in at.button if b.label == "Отклонить").click().run()

    with Store(web_db["db"]) as store:
        assert store.receipt(web_db["receipt_id"])["review_status"] == "rejected"
        assert svc.report_with_pending(store, "2026-10-01", "2026-10-31")["currencies"] == {}


def test_cloud_button_is_disabled_without_provider_confirmation(web_db, monkeypatch):
    monkeypatch.setattr("mail_assistant.web_services.OPENCODE_GO_RECEIPTS_CONFIRMED", False)
    at = _app().run()

    recognize = next(b for b in at.button if b.label == "Распознать новые")
    assert recognize.disabled is True
    assert any("отключён" in info.value for info in at.info)


def test_recognize_refuses_models_outside_compatible_list(web_db):
    with Store(web_db["db"]) as store:
        with pytest.raises(Exception):
            svc.recognize_documents(store, "claude-haiku-5-5", [1])


def test_report_currencies_are_not_summed(web_db):
    with Store(web_db["db"]) as store:
        report = svc.report_with_pending(store, "2026-10-01", "2026-10-31")

    assert "RUB" not in report["currencies"]
    assert json.dumps(report["pending_rows"], default=str)


def test_first_screen_is_money_and_recipients_not_processing_diagnostics(web_db):
    at = _app().run()
    assert not at.exception
    assert at.tabs[0].label == "Расходы"
    assert any(metric.label == "Потрачено · RUB" and "900,00" in metric.value for metric in at.metric)
    assert any(metric.label == "Платежей" and str(metric.value) == "1" for metric in at.metric)
    assert not any(metric.label == "Загружено реальных документов" for metric in at.metric)
