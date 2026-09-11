# Тонкая обёртка над админским HTTP API FeedHeat Crowd (Django Ninja).
#
# Здесь нет бизнес-логики: только транспорт, заголовки, ретраи и перевод ошибок
# бэкенда в человекочитаемый текст. Логика «что и когда звать» живёт в server.py,
# чтобы клиент можно было тестировать без MCP-обвязки.
#
# Формат ошибок бэкенда фиксирован в config/errors.py: {"error": {"code", "message"}}.
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

DEFAULT_BASE_URL = 'https://app2.feedheat.com'
# 12с: генерации/скрапинга здесь нет, все ручки — обычные CRUD-запросы. Больше ждать
# смысла нет, MCP-клиент всё равно упрётся в собственный таймаут.
DEFAULT_TIMEOUT = 12.0
# Ретраим только идемпотентные GET. Три попытки = две повторных, дальше отдаём ошибку.
GET_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 502, 503, 504})

USER_AGENT = 'feedheat-mcp/0.1'


class ConfigError(Exception):
    """Сервер не настроен (нет ключа/битый URL) — чинится переменными окружения."""


class ApiError(Exception):
    """Ошибка бэкенда или транспорта, уже переведённая в текст для модели."""

    def __init__(self, status: int, code: str, message: str, *, hint: str = '') -> None:
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(self._text())

    def _text(self) -> str:
        head = f'[{self.status} {self.code}] {self.message}'
        return f'{head} — {self.hint}' if self.hint else head

    def __str__(self) -> str:
        return self._text()


def _hint_for(status: int, scope: str | None) -> str:
    """Подсказка модели, что делать с кодом ответа. 401/403 — самые частые у ключа."""
    if status == 401:
        return (
            'The API key was not accepted: it is revoked, expired, or FEEDHEAT_API_KEY holds a '
            'wrong value. This cannot be fixed from here — ask a FeedHeat admin to issue a new key '
            'and restart the MCP server. Do not retry.'
        )
    if status == 403:
        need = f' (needs scope "{scope}")' if scope else ''
        return (
            f'The key is valid but not allowed to do this{need}. Either the scope is missing, or '
            'the action is permanently out of reach for API keys: impersonating users, changing '
            'user status and processing payouts are admin-UI only by design. Do not retry.'
        )
    if status == 404:
        return 'Check the id — the listing tools on this server return current ids.'
    if status == 409:
        return 'The object is in a state that forbids this action (e.g. order already opened).'
    if status == 429:
        return 'Rate limited. Wait a few seconds before the next call.'
    if status >= 500:
        return 'Server-side failure. Safe to retry a read; do NOT blindly retry an order creation.'
    return ''


class AdminClient:
    """Асинхронный клиент админского API. Один экземпляр на процесс.

    transport подменяется в тестах (httpx.MockTransport) — сеть в тестах не нужна.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff: float = 0.4,
    ) -> None:
        url = (base_url or os.getenv('FEEDHEAT_API_URL') or DEFAULT_BASE_URL).strip().rstrip('/')
        key = (api_key or os.getenv('FEEDHEAT_API_KEY') or '').strip()
        if not key:
            raise ConfigError(
                'FEEDHEAT_API_KEY is not set. Add it to the MCP server config (env) — '
                'the key looks like fhk_... and is issued in the FeedHeat admin panel.'
            )
        if not url.startswith(('http://', 'https://')):
            raise ConfigError(f'FEEDHEAT_API_URL must start with http:// or https://, got: {url!r}')

        self.base_url = url
        self._key = key
        self._retry_backoff = retry_backoff
        self._http = httpx.AsyncClient(
            base_url=url,
            timeout=timeout,
            transport=transport,
            headers={
                'Authorization': f'Bearer {key}',
                'Accept': 'application/json',
                'User-Agent': USER_AGENT,
            },
        )

    def __repr__(self) -> str:
        # Ключ не печатаем никогда: repr клиента попадает в traceback'и и логи MCP-хоста.
        return f'<AdminClient base_url={self.base_url!r} key=***>'

    async def aclose(self) -> None:
        await self._http.aclose()

    def _scrub(self, text: str) -> str:
        """Последний рубеж: вычищаем ключ из любого текста, который уйдёт наружу."""
        if not text:
            return ''
        return text.replace(self._key, '***')

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        scope: str | None = None,
    ) -> Any:
        verb = method.upper()
        # Ретрай только на GET. POST /admin/orders неидемпотентен: повтор после таймаута
        # создаёт второй заказ, а за него платят реальные деньги.
        attempts = GET_ATTEMPTS if verb == 'GET' else 1
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        clean_json = None if json is None else {k: v for k, v in json.items() if v is not None}

        last: ApiError | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._http.request(
                    verb, path, params=clean_params or None, json=clean_json,
                )
            except httpx.TimeoutException:
                last = ApiError(
                    504, 'TIMEOUT',
                    f'{verb} {path} timed out.',
                    hint=(
                        'The read did not complete; retry is safe.' if verb == 'GET' else
                        'The write may still have been applied on the server. Do NOT repeat it '
                        'blindly — check with list_orders / get_order first.'
                    ),
                )
            except httpx.HTTPError as exc:
                # Сообщение httpx может содержать URL — ключ в URL не носим, но чистим на всякий.
                last = ApiError(
                    503, 'NETWORK',
                    self._scrub(f'Cannot reach {self.base_url}: {type(exc).__name__}: {exc}'),
                    hint='Check FEEDHEAT_API_URL and that the backend is up.',
                )
            else:
                if resp.status_code in RETRY_STATUSES and attempt < attempts:
                    await self._backoff(attempt)
                    continue
                return self._parse(resp, scope=scope)

            if attempt < attempts:
                await self._backoff(attempt)
        raise last  # type: ignore[misc]

    async def _backoff(self, attempt: int) -> None:
        if self._retry_backoff > 0:
            await asyncio.sleep(self._retry_backoff * attempt)

    def _parse(self, resp: httpx.Response, *, scope: str | None) -> Any:
        if resp.status_code == 204 or not resp.content:
            return None
        if resp.is_success:
            try:
                return resp.json()
            except ValueError:
                raise ApiError(
                    resp.status_code, 'BAD_RESPONSE',
                    'API returned a non-JSON body.',
                    hint=(
                        'FEEDHEAT_API_URL probably points at the SPA or a proxy error page '
                        'instead of the API root.'
                    ),
                ) from None

        code, message = self._error_fields(resp)
        raise ApiError(resp.status_code, code, message, hint=_hint_for(resp.status_code, scope))

    def _error_fields(self, resp: httpx.Response) -> tuple[str, str]:
        """{"error": {"code","message"}} → пара; иначе первые 200 символов тела."""
        try:
            payload = resp.json()
        except ValueError:
            body = self._scrub(resp.text or '').strip()
            return 'HTTP_ERROR', (body[:200] or resp.reason_phrase or 'Request failed')
        err = payload.get('error') if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return (
                str(err.get('code') or 'HTTP_ERROR'),
                self._scrub(str(err.get('message') or 'Request failed')),
            )
        return 'HTTP_ERROR', self._scrub(str(payload))[:200]

    # --- Чтение ---------------------------------------------------------------

    # Справочники клиентов/исполнителей на бэкенде размечены как orders:read
    # (это контекст заказа, а не работа с людьми) — users:read живёт на /admin/users,
    # которую этот сервер не трогает.
    async def get_customers(self) -> list[dict]:
        return await self.request('GET', '/api/admin/customers', scope='orders:read')

    async def get_customer_projects(self, customer_id: str) -> list[dict]:
        return await self.request(
            'GET', f'/api/admin/customers/{customer_id}/projects', scope='orders:read',
        )

    async def get_executors(self) -> list[dict]:
        return await self.request('GET', '/api/admin/executors', scope='orders:read')

    async def get_orders(self, *, status: str | None = None, customer_id: str | None = None):
        return await self.request(
            'GET', '/api/orders/',
            params={'status': status, 'customerId': customer_id},
            scope='orders:read',
        )

    async def get_order(self, order_id: str) -> dict:
        return await self.request('GET', f'/api/orders/{order_id}', scope='orders:read')

    async def get_order_candidates(self, order_id: str) -> dict:
        return await self.request(
            'GET', f'/api/admin/orders/{order_id}/in-scope-executors', scope='orders:read',
        )

    # --- Запись ---------------------------------------------------------------

    async def create_order(self, payload: dict) -> dict:
        return await self.request('POST', '/api/admin/orders', json=payload, scope='orders:write')

    async def create_orders_batch(self, payload: dict) -> dict:
        return await self.request(
            'POST', '/api/admin/orders/batch', json=payload, scope='orders:write',
        )

    async def assign_order(self, order_id: str, payload: dict) -> dict:
        return await self.request(
            'POST', f'/api/admin/orders/{order_id}/assign', json=payload, scope='orders:write',
        )

    async def open_order(self, order_id: str) -> dict:
        return await self.request(
            'POST', f'/api/admin/orders/{order_id}/open', json={}, scope='orders:write',
        )

    async def cancel_order(self, order_id: str) -> dict:
        # Отмена живёт в общем роутере заказов (её умеет и заказчик), а не в /admin.
        return await self.request(
            'POST', f'/api/orders/{order_id}/cancel', json={}, scope='orders:write',
        )


class CustomerClient(AdminClient):
    """Тот же транспорт, но ручки — клиентские (/api/orders, /api/projects).

    Отдельным классом, а не набором методов в AdminClient, по одной причине:
    ключ заказчика физически не может позвать /api/admin/* (роль не та), и
    сервер, который ими не владеет, не должен их и уметь. Список методов здесь —
    это и есть граница того, что клиентский MCP вообще способен сделать.

    Сужения по учётке тут нет и быть не может: его делает бэкенд через
    workspace_owner_id. Подставить чужой customerId в параметрах нельзя —
    клиентская ветка /api/orders его не читает.
    """

    READ = 'client:read'
    WRITE = 'client:write'

    async def projects(self) -> list[dict]:
        return await self.request('GET', '/api/projects/', scope=self.READ)

    async def orders(self, *, status: str | None = None) -> list[dict]:
        return await self.request(
            'GET', '/api/orders/', params={'status': status}, scope=self.READ,
        )

    async def order(self, order_id: str) -> dict:
        return await self.request('GET', f'/api/orders/{order_id}', scope=self.READ)

    async def metrics_summary(self, *, days: int | None = None,
                              project_id: str | None = None) -> dict:
        return await self.request(
            'GET', '/api/orders/metrics-summary',
            params={'days': days, 'projectId': project_id}, scope=self.READ,
        )

    async def create_order(self, payload: dict) -> dict:
        return await self.request('POST', '/api/orders/', json=payload, scope=self.WRITE)

    async def update_order(self, order_id: str, payload: dict) -> dict:
        return await self.request(
            'PATCH', f'/api/orders/{order_id}', json=payload, scope=self.WRITE,
        )

    async def publish_order(self, order_id: str) -> dict:
        return await self.request(
            'POST', f'/api/orders/{order_id}/publish', json={}, scope=self.WRITE,
        )

    async def cancel_order(self, order_id: str) -> dict:
        return await self.request(
            'POST', f'/api/orders/{order_id}/cancel', json={}, scope=self.WRITE,
        )

    async def unpublish_order(self, order_id: str) -> dict:
        return await self.request(
            'POST', f'/api/orders/{order_id}/unpublish', json={}, scope=self.WRITE,
        )
