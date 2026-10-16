"""Десять критических сквозных сценариев. Подставной LLM — только внешняя граница облачного API.

Загрузка, распознавание, проверка, исправления, сохранение и итоги выполняются через UI.
Счётчик LLM и содержимое базы проверяются отдельно там, где браузер этого не доказывает.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from fake_llm import FakeLLM
from harness import AppServer
from ui import (
    db,
    document_id_by_name,
    expect_no_crash,
    metric,
    money,
    norm,
    open_app,
    pending_count,
    receipt_rows,
    recognize_all,
    recognize_once,
    select_review_document,
    set_period,
    upload,
)

OCTOBER = ("2026/10/01", "2026/10/16")


def _files(fixture_paths: dict[str, Path], *keys: str) -> list[Path]:
    return [fixture_paths[key] for key in keys]


# 1. Первый запуск и навигация

def test_s01_first_run_empty_state_and_layout(app_server: AppServer, page: Page) -> None:
    open_app(page, width=1440)
    for name in ("Расходы", "Документы", "Проверить записи", "Добавить чеки"):
        expect(page.get_by_role("tab", name=name)).to_be_visible()
    expect(page.get_by_text("пока нет распознанных платежей", exact=False)).to_be_visible()
    expect(metric(page, "Потрачено")).to_have_count(0)
    expect_no_crash(page, app_server)
    assert len(receipt_rows(app_server)) == 0

    page.set_viewport_size({"width": 800, "height": 900})
    expect(page.get_by_role("heading", name="Мои расходы")).to_be_visible()
    expect(page.get_by_role("tab", name="Добавить чеки")).to_be_visible()
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"горизонтальная прокрутка на 800 px: {overflow}"
    expect_no_crash(page, app_server)


# 2. PNG → понятный платёж

def test_s02_png_receipt_fields_and_original(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "loan_png"))
    recognize_all(page, app_server)
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800, "BYN"))
    expect(metric(page, "Платежей")).to_have_text("1")

    select_review_document(page, "ЗАО ТЕСТ КРЕДИТ")
    expect(page.get_by_label("Получатель / магазин", exact=True)).to_have_value("ЗАО ТЕСТ КРЕДИТ")
    expect(page.get_by_label("Дата платежа (YYYY-MM-DD)", exact=True)).to_have_value("2026-10-05")
    expect(page.get_by_label("Сумма платежа", exact=True)).to_have_value("2488.00")
    expect(page.get_by_label("Валюта", exact=True)).to_have_value("BYN")
    expect(page.get_by_label("Категория", exact=True)).to_have_value("Кредиты")
    expect(page.get_by_role("tabpanel", name="Проверить записи").get_by_text("Исходная квитанция", exact=True).first).to_be_visible()

    row = receipt_rows(app_server)[0]
    assert row["purpose"] == "Погашение кредита частичное"
    assert row["debit_minor"] == 248800
    assert row["intermediary"] == "ЕРИП"
    assert fake_llm.count("АЛЬФА") == 1
    expect_no_crash(page, app_server)


# 3. Текстовый PDF и PDF-скан

def test_s03_text_pdf_and_scanned_pdf(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "service_pdf", "store_scan"))
    recognize_all(page, app_server)

    text_doc = document_id_by_name(app_server, "e2e_service_fee.pdf")
    scan_doc = document_id_by_name(app_server, "e2e_store_scan.pdf")
    with db(app_server) as conn:
        text_row = conn.execute("SELECT * FROM documents WHERE id = ?", (text_doc,)).fetchone()
        scan_row = conn.execute("SELECT * FROM documents WHERE id = ?", (scan_doc,)).fetchone()
    assert "источник: pdf_text" in text_row["extracted_text"] and "источник: ocr" not in text_row["extracted_text"]
    assert "источник: ocr" in scan_row["extracted_text"] and "источник: pdf_text" not in scan_row["extracted_text"]
    assert text_row["status"] == "ai_done" and scan_row["status"] == "ai_done"

    by_document = {row["document_id"]: row for row in receipt_rows(app_server)}
    service = by_document[text_doc]
    assert (service["amount_minor"], service["currency"], service["purchase_date"]) == (5700, "BYN", "2026-10-08")
    scan = by_document[scan_doc]
    assert (scan["amount_minor"], scan["currency"], scan["purchase_date"], scan["category"]) == (3140, "BYN", "2026-10-11", "food")

    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(5700 + 3140, "BYN"))
    select_review_document(page, "ОАО ТЕСТ СВЯЗЬ")
    expect(page.get_by_text(re.compile("PDF · страница 1 из 1")).first).to_be_visible()
    select_review_document(page, "ТЕСТМАРКЕТ")
    expect(page.get_by_text(re.compile("PDF · страница 1 из 1")).first).to_be_visible()
    expect_no_crash(page, app_server)


# 4. Неполные данные не скрываются

def test_s04_incomplete_data_is_visible_and_not_confirmable(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "no_currency_png", "no_amount_png", "loan_png"))
    recognize_all(page, app_server)

    select_review_document(page, "ЖКХ ТЕСТ")
    expect(page.get_by_role("heading", name="19,90 (валюта не определена)")).to_be_visible()
    expect(page.get_by_test_id("stAlertContentWarning").get_by_text("Не определена валюта")).to_be_visible()
    expect(page.get_by_role("button", name="Подтвердить")).to_be_disabled()

    select_review_document(page, "ТЕСТ ПАРКОВКА")
    expect(page.get_by_text("Сумма не распознана").first).to_be_visible()
    expect(page.get_by_role("button", name="Подтвердить")).to_be_disabled()

    select_review_document(page, "ЗАО ТЕСТ КРЕДИТ")
    expect(page.get_by_text("ИНН", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="Подтвердить")).to_be_enabled()
    page.get_by_role("button", name="Подтвердить").click()
    expect(page.get_by_text("Расход подтверждён")).to_be_visible()

    rows = {row["document_id"]: row for row in receipt_rows(app_server)}
    assert rows[document_id_by_name(app_server, "e2e_no_currency.png")]["currency"] is None
    assert rows[document_id_by_name(app_server, "e2e_no_amount.png")]["amount_minor"] is None
    assert rows[document_id_by_name(app_server, "e2e_loan_payment.png")]["review_status"] == "confirmed"
    expect_no_crash(page, app_server)


# 5. Исправление и сохранение

def test_s05_correction_confirm_and_restart_keep_data(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "store_scan"))
    recognize_all(page, app_server)
    assert fake_llm.count("ГАММА") == 1

    select_review_document(page, "ТЕСТМАРКЕТ")
    page.get_by_label("Сумма платежа", exact=True).fill("35.00")
    page.get_by_label("Категория", exact=True).click()
    page.get_by_label("Категория", exact=True).fill("Дом")
    page.get_by_role("option", name="Дом", exact=True).click()
    page.get_by_label("За что / назначение платежа", exact=True).fill("Покупки для дома")
    page.get_by_role("button", name="Сохранить исправления").click()
    expect(page.get_by_text("Исправления сохранены")).to_be_visible()
    page.get_by_role("button", name="Подтвердить").click()
    expect(page.get_by_text("Расход подтверждён")).to_be_visible()

    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(3500, "BYN"))
    app_server.restart()
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(3500, "BYN"))

    row = receipt_rows(app_server)[0]
    assert (row["amount_minor"], row["debit_minor"], row["category"], row["review_status"]) == (3500, 3500, "household", "confirmed")
    assert row["purpose"] == "Покупки для дома"
    assert fake_llm.count("ГАММА") == 1, "исправление не должно вызывать LLM"
    expect_no_crash(page, app_server)


# 6. Повторная загрузка того же файла

def test_s06_same_file_twice_no_second_expense_no_llm(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "loan_png"))
    recognize_all(page, app_server)
    assert fake_llm.count("АЛЬФА") == 1

    upload(page, _files(fixture_paths, "loan_png"))
    assert pending_count(app_server) == 0
    open_app(page)
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800, "BYN"))
    expect(metric(page, "Платежей")).to_have_text("1")

    with db(app_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert len(receipt_rows(app_server)) == 1
    assert fake_llm.count("АЛЬФА") == 1, "повторная загрузка и перерисовка не должны вызывать LLM"
    expect_no_crash(page, app_server)


# 7. Дубли операции и похожие покупки

def test_s07_one_operation_from_two_documents_and_separate_similar_purchase(
    app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM
) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "loan_png", "loan_pdf_copy", "loan_second_png"))
    recognize_all(page, app_server)
    assert fake_llm.count("АЛЬФА") == 2
    assert fake_llm.count("БРАВО") == 1

    open_app(page)
    set_period(page, *OCTOBER)
    # Две разные операции ЕРИП: 700112233 (два документа) и 700998877 (отдельная покупка)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800 * 2, "BYN"))
    expect(metric(page, "Платежей")).to_have_text("2")
    select_review_document(page, "ЗАО ТЕСТ КРЕДИТ")
    expect(page.get_by_text("Возможный дубль")).to_have_count(0)

    rows = receipt_rows(app_server)
    assert len(rows) == 3, "три документа: PNG, PDF той же операции и вторая покупка"
    loan_operations = {row["operation_id"] for row in rows if row["amount_minor"] == 248800 and row["operation_id"]}
    assert len(loan_operations) == 1, "PNG и PDF одной операции ЕРИП должны стать одной операцией"
    expect_no_crash(page, app_server)


# 8. Комиссия и отток

def test_s08_fee_is_shown_separately_and_outflow_is_57(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "service_pdf"))
    recognize_all(page, app_server)
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(5700, "BYN"))
    select_review_document(page, "ОАО ТЕСТ СВЯЗЬ")
    expect(page.get_by_label("Сумма платежа", exact=True)).to_have_value("57.00")
    expect(page.get_by_role("tabpanel", name="Проверить записи").get_by_text("Комиссия", exact=False).first).to_be_visible()
    page.get_by_text("Цитаты и подробности распознавания").click()
    expect(page.get_by_text("fee: 2.00")).to_be_visible()

    row = receipt_rows(app_server)[0]
    assert (row["amount_minor"], row["debit_minor"], row["fee_minor"]) == (5700, 5700, 200)
    expect_no_crash(page, app_server)


# 9. Период, валюты, возврат и счёт

def test_s09_period_boundaries_currencies_refund_and_bill(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    keys = ("boundary_0930", "boundary_0101", "boundary_1016", "boundary_1017", "boundary_refund",
            "refund_png", "usd_png", "invoice_pdf")
    open_app(page)
    upload(page, _files(fixture_paths, *keys))
    recognize_all(page, app_server)

    open_app(page)
    set_period(page, *OCTOBER)
    # BYN: 22,22 (01.10, граница) + 33,33 (16.10, граница); 11,11 (30.09) и 44,44 (17.10) вне периода
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(2222 + 3333, "BYN"))
    expect(page.get_by_text(f"Возвращено {money(500, 'BYN')} · чистый отток {money(2222 + 3333 - 500, 'BYN')}")).to_be_visible()
    # USD раздельно: 12,50 расход, 25,00 возврат
    expect(metric(page, "Потрачено · USD")).to_have_text(money(1250, "USD"))
    expect(page.get_by_text(f"Возвращено {money(2500, 'USD')} · чистый отток -{money(1250, 'USD')}")).to_be_visible()
    # Счёт без оплаты не в расходах
    expect(page.get_by_text("Счета без подтверждения оплаты · 1")).to_be_visible()
    expect(page.get_by_text("Потрачено · EUR")).to_have_count(0)
    expect(page.get_by_text(re.compile(r"Итого[^\n]*(BYN|USD)[^\n]*(BYN|USD)"))).to_have_count(0)

    with db(app_server) as conn:
        invoice = conn.execute("SELECT payment_status FROM receipts WHERE document_id = ?",
                               (document_id_by_name(app_server, "e2e_invoice_unpaid.pdf"),)).fetchone()
    assert invoice["payment_status"] == "unpaid"
    expect_no_crash(page, app_server)


# 10. Ошибка AI и восстановление

def test_s10_ai_invalid_and_timeout_then_explicit_retry(app_server: AppServer, page: Page, fixture_paths, fake_llm: FakeLLM) -> None:
    open_app(page)
    upload(page, _files(fixture_paths, "store_scan"))
    recognize_all(page, app_server)
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(3140, "BYN"))

    fake_llm.queue_modes("АЛЬФА", "invalid")
    fake_llm.queue_modes("ВЕГА", "timeout")
    upload(page, _files(fixture_paths, "loan_png", "service_pdf"))
    recognize_once(page)
    expect_no_crash(page, app_server)

    loan_doc = document_id_by_name(app_server, "e2e_loan_payment.png")
    fee_doc = document_id_by_name(app_server, "e2e_service_fee.pdf")
    with db(app_server) as conn:
        loan = conn.execute("SELECT status, error_code FROM documents WHERE id = ?", (loan_doc,)).fetchone()
        fee = conn.execute("SELECT status, error_code FROM documents WHERE id = ?", (fee_doc,)).fetchone()
    assert (loan["status"], loan["error_code"]) == ("text_extracted", "LLM_INVALID")
    assert (fee["status"], fee["error_code"]) == ("text_extracted", "LLM_UNCERTAIN")
    assert fake_llm.count("АЛЬФА") == 1 and fake_llm.count("ВЕГА") == 1
    assert not [row for row in receipt_rows(app_server) if row["document_id"] in (loan_doc, fee_doc)]

    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(3140, "BYN"), timeout=30_000)

    # Таймаут с неизвестным исходом не предлагается к повтору и не повторяется сам; неверный ответ — можно
    assert pending_count(app_server) == 1
    assert fake_llm.count("ВЕГА") == 1

    # Явный повтор после неверного ответа: один корректный результат, без дубля
    recognize_once(page)
    assert fake_llm.count("ВЕГА") == 1, "таймаут не должен повторяться автоматически"
    assert fake_llm.count("АЛЬФА") == 2
    assert len([row for row in receipt_rows(app_server) if row["document_id"] == loan_doc]) == 1
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(3140 + 248800, "BYN"))
    expect(metric(page, "Платежей")).to_have_text("2")
    expect_no_crash(page, app_server)


@pytest.mark.parametrize("width", [1440, 800])
def test_s01_layout_keeps_amounts_visible(app_server: AppServer, page: Page, fixture_paths, width: int) -> None:
    open_app(page, width=width)
    upload(page, _files(fixture_paths, "store_scan"))
    recognize_all(page, app_server)
    open_app(page, width=width)
    set_period(page, *OCTOBER)
    value = metric(page, "Потрачено · BYN")
    expect(value).to_be_visible()
    box = value.bounding_box()
    viewport = page.viewport_size
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"], f"сумма обрезана при {width} px"
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1
    expect(page.get_by_role("tab", name="Добавить чеки")).to_be_visible()
    assert norm(value.inner_text()) == money(3140, "BYN")
