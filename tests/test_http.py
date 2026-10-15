# Сетевой MCP: один процесс на всех, ключ — из заголовка каждого запроса.
from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest

from feedheat_mcp import client as client_mod
from feedheat_mcp import client_server
from feedheat_mcp import http as http_mod
from feedheat_mcp import server as admin_server
from feedheat_mcp.client import AdminClient, CustomerClient

SEEN: list[str] = []


def backend(request: httpx.Request) -> httpx.Response:
    SEEN.append(request.headers.get('authorization', ''))
    return httpx.Response(200, json=[])


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """Клиенты под ключ ходят не в сеть, а в backend() — и запоминают ключ."""
    SEEN.clear()
    client_mod._KEYED.clear()
    admin_server.set_client(None)
    client_server.set_client(None)

    def keyed(cls, key):
        return cls(api_key=key, base_url='https://api.test',
                   transport=httpx.MockTransport(backend), retry_backoff=0.0)

    monkeypatch.setattr(admin_server, 'keyed_client', keyed)
    monkeypatch.setattr(client_server, 'keyed_client', keyed)
    yield


@asynccontextmanager
async def serve():
    """Приложение с запущенными менеджерами сессий — внутри теста, а не в фикстуре:
    группа задач SDK не переживает переход из задачи фикстуры в задачу теста."""
    app = http_mod.create_app()
    async with AsyncExitStack() as stack:
        for manager, _inner in app.routes.values():
            await stack.enter_async_context(manager.run())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='https://taskospot.test') as c:
            yield c


def rpc(method, params=None, id_=1):
    return {'jsonrpc': '2.0', 'id': id_, 'method': method, 'params': params or {}}


HEADERS = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}


async def call(http, path, key, method, params=None):
    headers = dict(HEADERS, **({'Authorization': f'Bearer {key}'} if key else {}))
    return await http.post(path, headers=headers, content=json.dumps(rpc(method, params)))


async def test_no_key_is_401():
    async with serve() as http:
        r = await call(http, '/mcp', '', 'tools/list')
        assert r.status_code == 401
        assert 'Bearer fhk_' in r.json()['error']


async def test_client_and_admin_paths_serve_their_own_tools():
    async with serve() as http:
        client_tools = (await call(http, '/mcp', 'fhk_a', 'tools/list')).json()['result']['tools']
        admin_tools = (await call(http, '/mcp/admin', 'fhk_a', 'tools/list')).json()['result']['tools']
        assert 'projects' in {t['name'] for t in client_tools}
        assert 'list_clients' in {t['name'] for t in admin_tools}
        assert 'list_clients' not in {t['name'] for t in client_tools}


async def test_each_request_uses_its_own_key():
    async with serve() as http:
        for key in ('fhk_alice', 'fhk_bob'):
            r = await call(http, '/mcp', key, 'tools/call', {'name': 'projects', 'arguments': {}})
            assert r.status_code == 200, r.text
        assert SEEN == ['Bearer fhk_alice', 'Bearer fhk_bob']


async def test_trailing_slash_and_health():
    async with serve() as http:
        r = await call(http, '/mcp/', 'fhk_a', 'tools/list')
        assert r.status_code == 200
        assert (await http.get('/healthz')).json() == {'ok': True}


def test_keyed_clients_are_reused_and_bounded(monkeypatch):
    monkeypatch.setattr(client_mod, '_KEYED_MAX', 2)
    client_mod._KEYED.clear()
    a = client_mod.keyed_client(CustomerClient, 'fhk_1')
    assert client_mod.keyed_client(CustomerClient, 'fhk_1') is a
    assert client_mod.keyed_client(AdminClient, 'fhk_1') is not a
    client_mod.keyed_client(CustomerClient, 'fhk_2')
    assert len(client_mod._KEYED) == 2
