"""Главный сквозной happy path: от загрузки до подтверждения и итога, через UI.

Два документа: платёж ЕРИП за кредит (PNG, 2488 BYN) и счёт за услугу (текстовый PDF, 55 BYN + комиссия 2 BYN = 57 BYN).
Ожидаемый итог за октябрь: 2488 + 57 = 2545 BYN.
Запись видео: pytest --video on (см. tests/e2e/README.md).
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

from fake_llm import FakeLLM
from harness import AppServer
from ui import (
    db,
    expect_no_crash,
    metric,
    money,
    open_app,
    recognize_all,
    select_review_document,
    set_period,
    upload,
)

OCTOBER = ("2026/10/01", "2026/10/16")


def test_happy_path_upload_recognize_confirm_total_reload(
    app_server: AppServer, page: Page, fixture_paths: dict[str, Path], fake_llm: FakeLLM
) -> None:
    # 1. Загрузка двух документов через UI
    open_app(page)
    upload(page, [fixture_paths["loan_png"], fixture_paths["service_pdf"]])

    # 2. Распознавание через UI; подставной LLM отвечает заранее заданными ответами
    recognize_all(page, app_server)
    assert fake_llm.count("АЛЬФА") == 1
    assert fake_llm.count("ОРИОН") == 1

    # 3. Проверка и подтверждение каждой записи через UI
    select_review_document(page, "ЗАО ТЕСТ КРЕДИТ")
    expect(page.get_by_label("Сумма платежа", exact=True)).to_have_value("2488.00")
    page.get_by_role("button", name="Подтвердить").click()
    expect(page.get_by_text("Расход подтверждён")).to_be_visible()

    select_review_document(page, "ОАО ТЕСТ СВЯЗЬ")
    expect(page.get_by_label("Сумма платежа", exact=True)).to_have_value("57.00")
    page.get_by_role("button", name="Подтвердить").click()
    expect(page.get_by_text("Расход подтверждён")).to_be_visible()

    # 4. Итог за период через UI: 2488 + 57 = 2545 BYN, комиссия не прибавляется повторно
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800 + 5700, "BYN"))
    expect(metric(page, "Платежей")).to_have_text("2")

    # 5. Перезагрузка страницы: итог и подтверждения сохранились, LLM не вызывается снова
    page.reload()
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800 + 5700, "BYN"))
    assert fake_llm.count() == 2, "перезагрузка не должна вызывать LLM"

    with db(app_server) as conn:
        confirmed = conn.execute("SELECT COUNT(*) FROM receipts WHERE review_status = 'confirmed'").fetchone()[0]
    assert confirmed == 2
    expect_no_crash(page, app_server)
