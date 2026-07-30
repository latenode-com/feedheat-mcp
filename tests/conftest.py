# Общая обвязка тестов: клиент поверх httpx.MockTransport — сеть не нужна.
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from feedheat_mcp.client import AdminClient

TEST_KEY = 'fhk_secret_test_key_do_not_leak'
TEST_URL = 'https://app2.example.test'


class Recorder:
    """Записывает запросы, отдаёт заранее заданные ответы."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def json_body(self, index: int = -1) -> Any:
        import json as _json
        return _json.loads(self.requests[index].content)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> tuple[AdminClient, Recorder]:
    recorder = Recorder(handler)
    client = AdminClient(
        base_url=kwargs.pop('base_url', TEST_URL),
        api_key=kwargs.pop('api_key', TEST_KEY),
        transport=httpx.MockTransport(recorder),
        retry_backoff=0.0,  # тесты не спят
        **kwargs,
    )
    return client, recorder


def json_response(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def api_error(status: int, code: str, message: str):
    return json_response({'error': {'code': code, 'message': message}}, status)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Переменные окружения разработчика не должны влиять на тесты."""
    monkeypatch.delenv('FEEDHEAT_API_KEY', raising=False)
    monkeypatch.delenv('FEEDHEAT_API_URL', raising=False)


@pytest.fixture(autouse=True)
def _reset_server_client():
    """Синглтон клиента в server.py — общий на процесс, сбрасываем между тестами."""
    from feedheat_mcp import server as srv

    srv.set_client(None)
    yield
    srv.set_client(None)
