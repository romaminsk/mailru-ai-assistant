"""Проверка тестового стенда: сетевой guard, изоляция процесса, запуск интерфейса."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Page, expect

from fake_llm import FakeLLM

E2E_DIR = Path(__file__).resolve().parent


def test_network_guard_blocks_external_and_keeps_loopback(fake_llm: FakeLLM) -> None:
    code = (
        "import sys, socket; sys.path.insert(0, sys.argv[1]); import netguard; netguard.install();\n"
        "try:\n"
        "    socket.create_connection(('93.184.216.34', 443), timeout=2)\n"
        "except OSError as exc:\n"
        "    print('BLOCKED', 'E2E' in str(exc))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('не заблокировано')\n"
    )
    blocked = subprocess.run([sys.executable, "-c", code, str(E2E_DIR)], capture_output=True, text=True, timeout=30)
    assert blocked.returncode == 0, blocked.stderr
    assert "BLOCKED True" in blocked.stdout

    loopback = (
        "import sys, socket, urllib.request; sys.path.insert(0, sys.argv[1]); import netguard; netguard.install();\n"
        "print(urllib.request.urlopen(sys.argv[2] + '/v1/models', timeout=5).status)\n"
    )
    allowed = subprocess.run(
        [sys.executable, "-c", loopback, str(E2E_DIR), fake_llm.base_url], capture_output=True, text=True, timeout=30
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "200"


def test_app_starts_isolated_and_renders(app_server, page: Page) -> None:
    page.goto("/")
    expect(page.get_by_role("heading", name="Мои расходы")).to_be_visible()
    expect(page.get_by_role("tab", name="Расходы")).to_be_visible()
    assert "Traceback" not in app_server.log_text()
    assert app_server.db_path.parent == app_server.run_dir
