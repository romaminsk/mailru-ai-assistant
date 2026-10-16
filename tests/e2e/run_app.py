"""Запуск тестового Streamlit-интерфейса на отдельном порту.

Окружение (пути БД и оригиналов, подставной LLM, отсутствие .env) задаёт harness.py.
Этот скрипт только включает сетевой guard и передаёт управление Streamlit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import netguard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8502)
    args = parser.parse_args()
    if args.port == 8501:
        raise SystemExit("Порт 8501 занят рабочим интерфейсом; тесты используют отдельный порт")
    netguard.install()
    from streamlit.web.cli import main as streamlit_main

    sys.argv = [
        "streamlit", "run", str(PROJECT_ROOT / "mail_assistant" / "web.py"),
        "--server.address", "127.0.0.1", "--server.port", str(args.port),
        "--server.headless", "true", "--server.fileWatcherType", "none",
        "--browser.gatherUsageStats", "false", "--global.developmentMode", "false",
    ]
    return int(streamlit_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
