"""Главный сквозной happy path: от загрузки до подтверждения и итога, через UI.

Два документа: платёж ЕРИП за кредит (PNG, 2488 BYN) и счёт за услугу (текстовый PDF, 55 BYN + комиссия 2 BYN = 57 BYN).
Ожидаемый итог за октябрь: 2488 + 57 = 2545 BYN.
Запись видео: pytest --video on (см. tests/e2e/README.md).
"""

from __future__ import annotations

import os
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
DEMO = os.environ.get("E2E_DEMO") == "1"
DEMO_PAUSE_MS = 2500


def narrate(page: Page, title: str, text: str) -> None:
    """Демо-режим: подпись поверх страницы, чтобы в видео было понятно, что происходит и зачем."""
    if not DEMO:
        return
    page.evaluate(
        """([title, text]) => {
            let box = document.getElementById('e2e-caption');
            if (!box) {
                box = document.createElement('div');
                box.id = 'e2e-caption';
                box.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:99999;'
                    + 'max-width:820px;padding:14px 20px;border-radius:12px;background:rgba(20,24,40,0.92);'
                    + 'color:#fff;font:15px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.35);pointer-events:none';
                document.body.appendChild(box);
            }
            box.innerHTML = '';
            const h = document.createElement('div');
            h.style.cssText = 'font-weight:700;font-size:16px;margin-bottom:4px';
            h.textContent = title;
            const t = document.createElement('div');
            t.textContent = text;
            box.append(h, t);
        }""",
        [title, text],
    )
    page.wait_for_timeout(DEMO_PAUSE_MS)


def test_happy_path_upload_recognize_confirm_total_reload(
    app_server: AppServer, page: Page, fixture_paths: dict[str, Path], fake_llm: FakeLLM
) -> None:
    narrate(page, "Цель приложения",
            "«Мои расходы» превращает квитанции PNG и PDF в учёт расходов: сумма, получатель, назначение, период.")
    open_app(page)

    # 1. Загрузка двух документов через UI
    narrate(page, "Шаг 1 · Загрузка",
            "Пользователь загружает квитанцию ЕРИП (PNG) и счёт за услугу (PDF). Файлы сохраняются локально.")
    upload(page, [fixture_paths["loan_png"], fixture_paths["service_pdf"]])

    # 2. Распознавание через UI; подставной LLM отвечает заранее заданными ответами
    narrate(page, "Шаг 2 · Распознавание",
            "Текст извлекается локально (OCR или слой PDF). Облачная модель разбирает текст в поля. Тестовый LLM подставлен.")
    recognize_all(page, app_server)
    assert fake_llm.count("АЛЬФА") == 1
    assert fake_llm.count("ОРИОН") == 1

    # 3. Проверка и подтверждение каждой записи через UI
    narrate(page, "Шаг 3 · Проверка",
            "Пользователь открывает первую запись: сумма 2488 BYN, получатель ЗАО ТЕСТ КРЕДИТ. Сверяет с оригиналом и подтверждает.")
    select_review_document(page, "ЗАО ТЕСТ КРЕДИТ")
    expect(page.get_by_label("Сумма платежа", exact=True)).to_have_value("2488.00")
    page.get_by_role("button", name="Подтвердить").click()
    expect(page.get_by_text("Расход подтверждён")).to_be_visible()

    narrate(page, "Шаг 3 · Проверка (2)",
            "Вторая запись: счёт за услугу 55 BYN плюс комиссия 2 BYN. В списание попадает 57 BYN, комиссия не считается дважды.")
    select_review_document(page, "ОАО ТЕСТ СВЯЗЬ")
    expect(page.get_by_label("Сумма платежа", exact=True)).to_have_value("57.00")
    page.get_by_role("button", name="Подтвердить").click()
    expect(page.get_by_text("Расход подтверждён")).to_be_visible()

    # 4. Итог за период через UI: 2488 + 57 = 2545 BYN, комиссия не прибавляется повторно
    narrate(page, "Шаг 4 · Итог",
            "Вкладка «Расходы» за период показывает итог по подтверждённым платежам: 2 545,00 BYN за два платежа.")
    open_app(page)
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800 + 5700, "BYN"))
    expect(metric(page, "Платежей")).to_have_text("2")

    # 5. Перезагрузка страницы: итог и подтверждения сохранились, LLM не вызывается снова
    narrate(page, "Шаг 5 · Перезагрузка",
            "Страница перезагружается. Итог и подтверждения остаются, повторного запроса к облачной модели нет.")
    page.reload()
    set_period(page, *OCTOBER)
    expect(metric(page, "Потрачено · BYN")).to_have_text(money(248800 + 5700, "BYN"))
    assert fake_llm.count() == 2, "перезагрузка не должна вызывать LLM"

    with db(app_server) as conn:
        confirmed = conn.execute("SELECT COUNT(*) FROM receipts WHERE review_status = 'confirmed'").fetchone()[0]
    assert confirmed == 2
    narrate(page, "Готово", "Сценарий пройден: от загрузки до итога за период, без повторных платных запросов.")
    expect_no_crash(page, app_server)
