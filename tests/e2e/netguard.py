"""Сетевой guard для тестового процесса приложения: разрешён только localhost.

Блокирует DNS-запросы и исходящие TCP-соединения к любым другим адресам (почта, LLM, интернет).
Соединения к 127.0.0.1 (Streamlit, браузер, подставной LLM) не затрагиваются.
"""

from __future__ import annotations

import socket

ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class BlockedNetworkError(OSError):
    pass


def check_address(sock: socket.socket, address: object) -> None:
    if sock.family == socket.AF_UNIX or not isinstance(address, tuple) or not address:
        return
    if str(address[0]) not in ALLOWED_HOSTS:
        raise BlockedNetworkError("E2E: исходящее соединение заблокировано")


def install() -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        check_address(self, address)
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        check_address(self, address)
        return original_connect_ex(self, address)

    def getaddrinfo(host: object, *args: object, **kwargs: object):
        if host is not None and str(host) not in ALLOWED_HOSTS:
            raise socket.gaierror("E2E: DNS-запросы заблокированы")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
