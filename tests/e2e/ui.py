"""Общие действия и проверки для сквозных сценариев. Ожидания — по состоянию, без фиксированных пауз."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, expect

from harness import AppServer

SPACES = re.compile(r"[\s\u00a0\u202f]+")
FILE_INPUT = 'input[type="file"]'  # единственное поле загрузки Streamlit; это не CSS-класс приложения


def norm(text: str) -> str:
    return SPACES.sub(" ", text).strip()


def money(minor: int, currency: str) -> str:
    """Сумма в формате интерфейса: «2 488,00 BYN» (пробел-разделитель тысяч, запятая для копеек)."""
    return f"{minor / 100:,.2f}".replace(",", " ").replace(".", ",") + f" {currency}"


def wait_until(predicate: Callable[[], Any], message: str, timeout: float = 120.0) -> Any:
    """Ожидание наблюдаемого состояния (например, строки в БД), а не фиксированная пауза."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"Не дождались состояния: {message}")
        time.sleep(0.25)


def db(app: AppServer) -> sqlite3.Connection:
    """Только чтение тестовой базы: проверка сохранности там, где браузер не доказывает запись."""
    conn = sqlite3.connect(f"file:{app.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_app(page: Page, width: int = 1440, height: int = 900) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/")
    expect(page.get_by_role("heading", name="Мои расходы")).to_be_visible()


def set_period(page: Page, start: str, end: str) -> None:
    """Период в формате YYYY/MM/DD: поле Streamlit — три spinbutton (год, месяц, день)."""
    for label, value in (("С", start), ("По", end)):
        for part, text in zip(("year", "month", "day"), value.split("/")):
            spin = page.get_by_role("spinbutton", name=f"{part}, {label}", exact=True)
            spin.fill(str(int(text)))
            spin.press("Enter")
            page.get_by_role("heading", name="Мои расходы").click()  # закрывает календарь без отмены значения
            expect(spin).to_have_text(re.compile(rf"^0*{int(text)}\s*$"))


def open_tab(page: Page, name: str) -> None:
    """Ждёт состояния «вкладка выбрана». Перерисовка Streamlit может сбросить выбор, поэтому клик повторяется."""
    tab = page.get_by_role("tab", name=name)
    for _ in range(3):
        tab.click()
        try:
            expect(page.get_by_role("tab", name=name, selected=True)).to_be_visible(timeout=4000)
            return
        except AssertionError:
            continue
    expect(page.get_by_role("tab", name=name, selected=True)).to_be_visible()


def upload(page: Page, paths: Sequence[Path]) -> None:
    open_tab(page, "Добавить чеки")
    page.locator(FILE_INPUT).set_input_files([str(path) for path in paths])
    open_tab(page, "Добавить чеки")  # загрузка перерисовывает страницу и сбрасывает выбранную вкладку
    button = page.get_by_role("button", name="Загрузить файлы")
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_text("Файлы сохранены. Запустите распознавание ниже.")).to_be_visible()


def pending_count(app: AppServer) -> int:
    """Документы в очереди распознавания по состоянию базы (как pending_documents). Список Streamlit
    скрывает уже выбранные варианты, поэтому по нему считать нельзя."""
    with db(app) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM documents d WHERE d.extracted_text IS NOT NULL AND d.status != 'ai_done' "
            "AND d.is_synthetic = 0 AND COALESCE(d.error_code, '') NOT IN ('NOT_RECEIPT', 'NO_TEXT', 'LLM_UNCERTAIN') "
            "AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.document_id = d.id AND r.is_current = 1)"
        ).fetchone()[0]


def recognize_once(page: Page) -> None:
    """Одно нажатие «Распознать новые» (до 3 документов). Перерисовка страницы ничего не запускает."""
    open_app(page)
    open_tab(page, "Добавить чеки")
    page.get_by_role("button", name="Распознать новые").click()
    expect(page.get_by_text("Обработка завершена. Результаты — во вкладке проверки.")).to_be_visible(timeout=180_000)


def recognize_all(page: Page, app: AppServer, expected_pending: int = 0) -> None:
    """Повторяет нажатия, пока остаются документы в очереди. Каждое нажатие ждёт завершения обработки."""
    for _ in range(10):
        if pending_count(app) == 0:
            break
        recognize_once(page)
    left = pending_count(app)
    if left != expected_pending:
        with db(app) as conn:
            rows = conn.execute("SELECT id, status, error_code FROM documents ORDER BY id").fetchall()
        state = "; ".join(f"{r['id']}:{r['status']}/{r['error_code']}" for r in rows)
        raise AssertionError(f"в очереди осталось {left} документов; состояние: {state}")


def metric(page: Page, label: str) -> Locator:
    return page.get_by_test_id("stMetric").filter(has_text=label).get_by_test_id("stMetricValue")


def select_review_document(page: Page, text: str) -> None:
    open_tab(page, "Проверить записи")
    page.get_by_label("Документ для проверки", exact=True).click()
    page.get_by_role("option", name=re.compile(re.escape(text))).first.click()
    open_tab(page, "Проверить записи")  # перерисовка после выбора может вернуть вкладку «Расходы»


def expect_no_crash(page: Page, app: AppServer) -> None:
    expect(page.get_by_text("Traceback")).to_have_count(0)
    expect(page.get_by_text(re.compile("Ошибка обработки"))).to_have_count(0)
    assert "Traceback" not in app.log_text()


def receipt_rows(app: AppServer, document_id: int | None = None) -> list[sqlite3.Row]:
    with db(app) as conn:
        if document_id is None:
            return conn.execute("SELECT * FROM receipts WHERE is_current = 1 ORDER BY id").fetchall()
        return conn.execute(
            "SELECT * FROM receipts WHERE is_current = 1 AND document_id = ? ORDER BY id", (document_id,)
        ).fetchall()


def document_id_by_name(app: AppServer, part_name: str) -> int:
    with db(app) as conn:
        row = conn.execute(
            "SELECT document_id FROM document_sources WHERE part_name = ? ORDER BY id LIMIT 1", (part_name,)
        ).fetchone()
    assert row is not None, f"документ {part_name} не найден в базе"
    return int(row["document_id"])
