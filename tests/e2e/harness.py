"""Управление тестовым Streamlit-процессом: отдельный порт, изолированные пути, очищенное окружение.

Процесс получает только явный allowlist переменных. Настоящий .env, почтовые и LLM-ключи
и рабочая база data/receipts.sqlite3 в него не попадают.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_APP = Path(__file__).resolve().parent / "run_app.py"
E2E_PORT = 8502
APP_URL = f"http://127.0.0.1:{E2E_PORT}"
INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "TESSDATA_PREFIX")
FAKE_KEY = "e2e-synthetic-key-not-a-secret"


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


class AppServer:
    """Один тестовый процесс. Пути данных задаются внутри run_dir."""

    def __init__(self, run_dir: Path, llm_base_url: str, *, llm_timeout_seconds: int = 3) -> None:
        self.run_dir = run_dir
        self.llm_base_url = llm_base_url
        self.llm_timeout_seconds = llm_timeout_seconds
        self.db_path = run_dir / "receipts.sqlite3"
        self.originals_dir = run_dir / "originals"
        self.log_path = run_dir / "streamlit.log"
        self._process: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None

    def environment(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
        env.update({
            "HOME": str(self.run_dir / "home"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Файл не существует: настоящий .env не загружается ни при каких условиях.
            "MAIL_ASSISTANT_ENV_FILE": str(self.run_dir / "no-such.env"),
            "RECEIPTS_DB_PATH": str(self.db_path),
            "RECEIPT_ORIGINALS_DIR": str(self.originals_dir),
            "LLM_API_KEY": FAKE_KEY,
            "LLM_API_URL": f"{self.llm_base_url}/v1/chat/completions",
            "LLM_MODELS_URL": f"{self.llm_base_url}/v1/models",
            "LLM_MODEL": "deepseek-v4.1-flash",
            "LLM_EMAIL_USE_CONFIRMED": "true",
            "LLM_TIMEOUT_SECONDS": str(self.llm_timeout_seconds),
            "RECEIPT_OCR_TIMEOUT_SECONDS": "60",
        })
        return env

    def start(self, timeout: float = 90.0) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not _port_is_free(E2E_PORT):
            raise RuntimeError(f"Порт {E2E_PORT} занят: тестовый интерфейс не может стартовать")
        (self.run_dir / "home").mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab")
        self._process = subprocess.Popen(
            [sys.executable, str(RUN_APP), "--port", str(E2E_PORT)],
            cwd=PROJECT_ROOT,
            env=self.environment(),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError(f"Тестовый интерфейс завершился при старте; см. {self.log_path}")
                if self._healthy():
                    return
                time.sleep(0.2)
            raise RuntimeError(f"Тестовый интерфейс не ответил за {timeout} с; см. {self.log_path}")
        except BaseException:
            self.stop()
            raise

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{APP_URL}/_stcore/health", timeout=2) as response:
                return response.read().decode().strip() == "ok"
        except Exception:
            return False

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=15)
        self._process = None
        if self._log is not None:
            self._log.close()
            self._log = None
        deadline = time.monotonic() + 15
        while not _port_is_free(E2E_PORT) and time.monotonic() < deadline:
            time.sleep(0.2)

    def restart(self) -> None:
        self.stop()
        self.start()

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
