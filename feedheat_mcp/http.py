"""Сетевой MCP: клиентский и админский серверы по HTTP на одном адресе.

Раньше MCP ставился у человека локально — uvx тянул пакет из публичного
зеркала на GitHub, ключ лежал в env. Владелец 15.10: «должен быть сетевой».
Теперь подключение — это адрес и ключ в заголовке:

    https://taskospot.com/mcp         — заказчик (feedheat-client-mcp)
    https://taskospot.com/mcp/admin   — модератор (feedheat-mcp)
    Authorization: Bearer fhk_…

Один процесс обслуживает всех: ключ берётся из заголовка КАЖДОГО запроса
(client.REQUEST_KEY), и что ключу можно, решает бэкенд, как и раньше. Режим
stateless: запрос — самостоятельный вызов, сессий между запросами нет, так что
процесс можно перезапускать и масштабировать без привязки клиента к реплике.
"""
from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack

from mcp.server.transport_security import TransportSecuritySettings

from feedheat_mcp import client_server
from feedheat_mcp import server as admin_server
from feedheat_mcp.client import REQUEST_KEY

# Защита от DNS rebinding нужна серверу на localhost, куда может постучаться
# чужая страница из браузера. Здесь сервер публичный и пускает только по ключу,
# а проверка Origin отрезала бы веб-клиентов (claude.ai шлёт свой Origin).
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)
_PATH = '/mcp'




async def _json(send, status: int, body: dict, headers: list | None = None) -> None:
    raw = json.dumps(body).encode()
    await send({'type': 'http.response.start', 'status': status, 'headers': [
        (b'content-type', b'application/json'), (b'content-length', str(len(raw)).encode()),
        *(headers or [])]})
    await send({'type': 'http.response.body', 'body': raw})


def _bearer(scope) -> str:
    for name, value in scope.get('headers') or []:
        if name == b'authorization':
            raw = value.decode('latin-1').strip()
            return raw[7:].strip() if raw[:7].lower() == 'bearer ' else ''
    return ''


def create_app():
    """ASGI-приложение. Фабрикой, а не модулем: менеджер сессий SDK запускается
    один раз на экземпляр, и каждый запуск (тест, перезапуск) берёт свежие."""
    routes = {}
    for path, server in (('/mcp/admin', admin_server.server), ('/mcp', client_server.server)):
        inner = server.streamable_http_app(streamable_http_path=_PATH, stateless_http=True,
                                           json_response=True, transport_security=_SECURITY)
        routes[path] = (server.session_manager, inner)

    async def lifespan(receive, send) -> None:
        stack = AsyncExitStack()
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                # Менеджер сессий держит группу задач, в которой живут запросы;
                # без него ни один вызов не обработается
                for manager, _inner in routes.values():
                    await stack.enter_async_context(manager.run())
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await stack.aclose()
                await send({'type': 'lifespan.shutdown.complete'})
                return

    async def app(scope, receive, send) -> None:
        if scope['type'] == 'lifespan':
            return await lifespan(receive, send)
        if scope['type'] != 'http':
            return
        path = scope['path'].rstrip('/') or '/'
        if path == '/healthz':
            return await _json(send, 200, {'ok': True})
        route = routes.get(path)
        if route is None:
            return await _json(send, 404, {'error': 'Not found. MCP lives at /mcp (clients) and /mcp/admin.'})
        key = _bearer(scope)
        if not key.startswith('fhk_'):
            return await _json(send, 401, {
                'error': 'API key required: send "Authorization: Bearer fhk_…". '
                         'Keys are issued in the Taskospot cabinet, on the AI agent screen.'},
                [(b'www-authenticate', b'Bearer')])
        token = REQUEST_KEY.set(key)
        try:
            await route[1]({**scope, 'path': _PATH, 'raw_path': _PATH.encode()}, receive, send)
        finally:
            REQUEST_KEY.reset(token)

    app.routes = routes
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=os.getenv('HOST', '0.0.0.0'),
                port=int(os.getenv('PORT', '8200')), proxy_headers=True,
                forwarded_allow_ips='*', log_level=os.getenv('LOG_LEVEL', 'info'))
