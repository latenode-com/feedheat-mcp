# Тесты транспорта: заголовки, ретраи, перевод ошибок, отсутствие утечки ключа.
from __future__ import annotations

import httpx
import pytest

from feedheat_mcp.client import DEFAULT_BASE_URL, AdminClient, ApiError, ConfigError
from tests.conftest import TEST_KEY, TEST_URL, api_error, json_response, make_client


async def test_get_builds_url_headers_and_query():
    client, rec = make_client(json_response([]))
    await client.get_orders(status='open')

    req = rec.last
    assert req.method == 'GET'
    assert str(req.url) == f'{TEST_URL}/api/orders/?status=open'
    assert req.headers['authorization'] == f'Bearer {TEST_KEY}'
    assert req.headers['accept'] == 'application/json'
    assert req.headers['user-agent'].startswith('feedheat-mcp/')
    await client.aclose()


async def test_none_query_params_are_dropped():
    client, rec = make_client(json_response([]))
    await client.get_orders()
    assert rec.last.url.query == b''
    await client.aclose()


async def test_none_body_fields_are_dropped():
    """None в payload = «не задано»; бэкенд сам подставит дефолт."""
    client, rec = make_client(json_response({'id': 'x'}, 201))
    await client.create_order({'type': 'post', 'title': None, 'rewardCents': 500})
    assert rec.json_body() == {'type': 'post', 'rewardCents': 500}
    await client.aclose()


async def test_post_targets_admin_orders_endpoint():
    client, rec = make_client(json_response({'id': 'x'}, 201))
    await client.create_order({'type': 'post'})
    assert rec.last.method == 'POST'
    assert rec.last.url.path == '/api/admin/orders'
    await client.aclose()


async def test_204_returns_none():
    client, _ = make_client(lambda _r: httpx.Response(204))
    assert await client.get_orders() is None
    await client.aclose()


async def test_error_body_is_translated():
    client, _ = make_client(api_error(400, 'VALIDATION', 'subreddit is required for post orders'))
    with pytest.raises(ApiError) as exc:
        await client.create_order({'type': 'post'})
    assert exc.value.status == 400
    assert exc.value.code == 'VALIDATION'
    assert 'subreddit is required' in str(exc.value)
    await client.aclose()


async def test_401_says_key_is_revoked_or_wrong():
    client, _ = make_client(api_error(401, 'UNAUTHORIZED', 'Authentication required'))
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    text = str(exc.value)
    assert '401' in text and 'revoked' in text
    assert 'Do not retry' in text
    await client.aclose()


async def test_403_names_the_missing_scope():
    client, _ = make_client(api_error(403, 'FORBIDDEN', 'Forbidden'))
    with pytest.raises(ApiError) as exc:
        await client.create_order({'type': 'post'})
    text = str(exc.value)
    assert 'orders:write' in text
    assert 'payouts' in text  # объясняем, что часть действий ключу закрыта навсегда
    await client.aclose()


async def test_403_scope_matches_the_backend_markup():
    """Справочники на бэкенде размечены orders:read, а не users:read — подсказка не врёт."""
    client, _ = make_client(api_error(403, 'SCOPE_DENIED', 'API key lacks required scope'))
    for call in (client.get_customers(), client.get_executors(), client.get_orders()):
        with pytest.raises(ApiError) as exc:
            await call
        assert 'orders:read' in str(exc.value)
    await client.aclose()


async def test_non_json_success_body_points_at_wrong_url():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<!doctype html><title>SPA</title>')

    client, _ = make_client(handler)
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    assert exc.value.code == 'BAD_RESPONSE'
    assert 'FEEDHEAT_API_URL' in str(exc.value)
    await client.aclose()


async def test_non_json_error_body_is_truncated():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text='x' * 5000)

    client, _ = make_client(handler)
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    assert len(exc.value.message) <= 200
    await client.aclose()


# --- Ключ не утекает --------------------------------------------------------------


async def test_key_is_scrubbed_from_error_message():
    """Даже если бэкенд эхом вернёт ключ в тексте ошибки — наружу он не уйдёт."""
    client, _ = make_client(api_error(400, 'VALIDATION', f'bad key {TEST_KEY} in payload'))
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    assert TEST_KEY not in str(exc.value)
    assert '***' in str(exc.value)
    await client.aclose()


async def test_key_is_scrubbed_from_non_json_error_body():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f'nginx echoed Authorization: Bearer {TEST_KEY}')

    client, _ = make_client(handler)
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    assert TEST_KEY not in str(exc.value)
    await client.aclose()


async def test_key_is_not_in_repr_or_network_error():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f'connection refused (tried key {TEST_KEY})')

    client, _ = make_client(handler)
    assert TEST_KEY not in repr(client)
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    assert TEST_KEY not in str(exc.value)
    await client.aclose()


# --- Ретраи -----------------------------------------------------------------------


async def test_get_retries_on_503_then_succeeds():
    state = {'n': 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        state['n'] += 1
        if state['n'] < 3:
            return httpx.Response(503, json={'error': {'code': 'X', 'message': 'down'}})
        return httpx.Response(200, json=[{'id': 'ok'}])

    client, rec = make_client(handler)
    assert await client.get_customers() == [{'id': 'ok'}]
    assert rec.calls == 3
    await client.aclose()


async def test_get_retries_on_transport_error_and_gives_up():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom')

    client, rec = make_client(handler)
    with pytest.raises(ApiError) as exc:
        await client.get_customers()
    assert rec.calls == 3
    assert exc.value.code == 'NETWORK'
    await client.aclose()


async def test_get_does_not_retry_on_400():
    client, rec = make_client(api_error(400, 'VALIDATION', 'nope'))
    with pytest.raises(ApiError):
        await client.get_customers()
    assert rec.calls == 1
    await client.aclose()


async def test_create_order_is_never_retried_on_transport_failure():
    """Деньги: повтор POST после падения = второй заказ и вторая выплата."""
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom')

    client, rec = make_client(handler)
    with pytest.raises(ApiError):
        await client.create_order({'type': 'post', 'rewardCents': 500})
    assert rec.calls == 1
    await client.aclose()


async def test_create_order_is_never_retried_on_503():
    client, rec = make_client(api_error(503, 'UNAVAILABLE', 'try later'))
    with pytest.raises(ApiError):
        await client.create_order({'type': 'post', 'rewardCents': 500})
    assert rec.calls == 1
    await client.aclose()


async def test_post_timeout_warns_that_write_may_have_landed():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('slow')

    client, rec = make_client(handler)
    with pytest.raises(ApiError) as exc:
        await client.create_orders_batch({'type': 'mass_post'})
    assert rec.calls == 1
    assert exc.value.code == 'TIMEOUT'
    assert 'Do NOT repeat it blindly' in str(exc.value)
    await client.aclose()


async def test_get_timeout_is_retried():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('slow')

    client, rec = make_client(handler)
    with pytest.raises(ApiError):
        await client.get_order('11111111-1111-1111-1111-111111111111')
    assert rec.calls == 3
    await client.aclose()


# --- Конфигурация -----------------------------------------------------------------


def test_missing_key_is_a_config_error():
    with pytest.raises(ConfigError) as exc:
        AdminClient()
    assert 'FEEDHEAT_API_KEY' in str(exc.value)


def test_key_and_url_come_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('FEEDHEAT_API_KEY', 'fhk_from_env')
    monkeypatch.setenv('FEEDHEAT_API_URL', 'http://localhost:8100/')
    client = AdminClient()
    assert client.base_url == 'http://localhost:8100'  # хвостовой слэш срезан


def test_default_url_is_production(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('FEEDHEAT_API_KEY', 'fhk_from_env')
    assert AdminClient().base_url == DEFAULT_BASE_URL


def test_bad_url_scheme_is_rejected():
    with pytest.raises(ConfigError):
        AdminClient(api_key='fhk_x', base_url='app2.feedheat.com')
