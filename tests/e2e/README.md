# Сквозные сценарии (Playwright + Streamlit)

Проверяют приложение через браузер. Облачный LLM заменён подставным сервером на localhost:
внешняя граница подменена, всё остальное (OCR, парсер ответа, проверка цитат, сохранение,
операции, итоги) настоящее. Подставной LLM не доказывает точность распознавания реальных документов.

## Подготовка

```bash
.venv/bin/pip install -e ".[e2e]"
.venv/bin/python -m playwright install chromium
```

Нужны Tesseract с языками `rus` и `eng` и шрифт с кириллицей (по умолчанию Arial;
путь можно задать переменной `E2E_FONT_PATH`).

## Запуск

```bash
.venv/bin/python -m pytest tests/e2e -p no:cacheprovider \
  --tracing retain-on-failure --screenshot only-on-failure --output test-results/e2e
```

Существующие модульные тесты (без браузера):

```bash
.venv/bin/python -m pytest tests --ignore=tests/e2e
```

## Изоляция

- Тестовый интерфейс: `http://127.0.0.1:8502`. Порт 8501 (рабочий) не используется.
- Процесс получает только явный allowlist переменных окружения. Настоящий `.env` не читается
  (`MAIL_ASSISTANT_ENV_FILE` указывает на несуществующий файл).
- База и оригиналы — во временном каталоге теста, рабочая `data/receipts.sqlite3` не открывается.
- Сетевой guard (`netguard.py`) блокирует исходящие соединения процесса приложения,
  кроме localhost.
- Фикстуры синтетические: вымышленные реквизиты, метка «Метка: СЛОВО» только для подставного LLM.

## Файлы

- `synthetic.py` — документы, ответы подставного LLM и эталон (`EXPECTED`).
- `fake_llm.py` — подставной LLM (режимы: ok, invalid, truncated, http500, timeout; счётчик вызовов).
- `harness.py`, `run_app.py`, `netguard.py` — запуск и изоляция процесса приложения.
- `ui.py` — общие действия через UI и чтение базы только для чтения.
- `test_scenarios.py` — десять сценариев.
- `test_infra.py` — проверка стенда.

Артефакты падений (скриншоты, трассы, лог Streamlit) — в `test-results/e2e` и `$TMPDIR` теста; в Git не попадают.
