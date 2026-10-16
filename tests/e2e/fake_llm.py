"""Подставной облачный LLM на localhost.

Это внешняя граница приложения: HTTP-вызов делает серверный Python (llm_client.py), а не браузер.
Весь остальной путь (OCR, парсер ответа, проверка цитат, сохранение, построение операций) настоящий.

Ответ выбирается по маркеру из текста документа (синтетический код вида E2E-LOAN).
Режимы поведения задаются очередью на маркер: ok, invalid, truncated, http500, timeout.
После исчерпания очереди действует ok. Каждый пришедший запрос учитывается.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODES = frozenset({"ok", "invalid", "truncated", "http500", "timeout"})
TIMEOUT_HOLD_SECONDS = 6.0  # дольше LLM_TIMEOUT_SECONDS тестового процесса (3 с)


@dataclass(frozen=True)
class LLMCall:
    marker: str | None
    mode: str
    model: str | None
    max_tokens: int | None


class FakeLLM:
    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self._modes: dict[str, list[str]] = {}
        self._calls: list[LLMCall] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def queue_modes(self, marker: str, *modes: str) -> None:
        for mode in modes:
            if mode not in MODES:
                raise ValueError(f"неизвестный режим LLM: {mode}")
        with self._lock:
            self._modes.setdefault(marker, []).extend(modes)

    def calls(self, marker: str | None = None) -> list[LLMCall]:
        with self._lock:
            return [call for call in self._calls if marker is None or call.marker == marker]

    def count(self, marker: str | None = None) -> int:
        return len(self.calls(marker))

    def reset(self) -> None:
        with self._lock:
            self._modes.clear()
            self._calls.clear()

    def _next_mode(self, marker: str | None) -> str:
        with self._lock:
            queue = self._modes.get(marker or "", [])
            return queue.pop(0) if queue else "ok"

    def _record(self, call: LLMCall) -> None:
        with self._lock:
            self._calls.append(call)

    def _marker_in(self, text: str) -> str | None:
        for marker in self.answers:
            if marker in text:
                return marker
        return None


def completion(content: str, *, finish: str = "stop", model: str = "deepseek-v4.1-flash") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
    }


def _make_handler(llm: FakeLLM) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - сигнатура http.server
            return

        def _send(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - имя метода задано http.server
            if self.path.endswith("/models"):
                self._send(200, {"data": [{"id": "deepseek-v4.1-flash"}]})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            text = "\n".join(
                str(message.get("content", "")) for message in body.get("messages", []) if message.get("role") == "user"
            )
            marker = llm._marker_in(text)
            mode = llm._next_mode(marker)
            llm._record(LLMCall(marker, mode, body.get("model"), body.get("max_tokens")))
            if mode == "http500":
                self._send(500, {"error": "synthetic"})
                return
            if mode == "timeout":
                time.sleep(TIMEOUT_HOLD_SECONDS)
                self._send(200, completion(json.dumps({"receipts": []})))
                return
            if mode == "invalid":
                self._send(200, completion("Извините, в документе не нашлось данных для JSON."))
                return
            if mode == "truncated":
                self._send(200, completion('{"receipts": [{"merchant_name": "ЗАП', finish="length"))
                return
            answer = llm.answers.get(marker or "", {"receipts": []})
            self._send(200, completion(json.dumps(answer, ensure_ascii=False), model=body.get("model") or "deepseek-v4.1-flash"))

    return Handler
