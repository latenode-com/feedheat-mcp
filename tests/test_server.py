# Тесты инструментов: единицы измерения (центы!), подтверждение перед тратой денег,
# валидация до сети, отсутствие ретрая на создании заказа.
from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from feedheat_mcp import server as srv
from feedheat_mcp.client import ApiError
from tests.conftest import TEST_KEY, api_error, json_response, make_client

CUSTOMER = '11111111-1111-1111-1111-111111111111'
PROJECT = '22222222-2222-2222-2222-222222222222'
EXECUTOR = '33333333-3333-3333-3333-333333333333'
ORDER = '44444444-4444-4444-4444-444444444444'


def use_client(handler):
    client, rec = make_client(handler)
    srv.set_client(client)
    return rec


def order_payload(**over):
    data = {
        'id': ORDER, 'type': 'post', 'status': 'open', 'subreddit': 'test',
        'title': 'Hi', 'body': 'text', 'bodyMode': 'text', 'instructions': '',
        'rewardCents': 500, 'quantity': 1, 'customerId': CUSTOMER, 'projectId': PROJECT,
        'assignedExecutorIds': [], 'assignedExecutorNames': [],
        'createdAt': '2026-07-29T10:00:00Z', 'updatedAt': '2026-07-29T10:00:00Z',
    }
    data.update(over)
    return data


# --- Единицы измерения ------------------------------------------------------------


def test_usd_formatting_is_cents_based():
    assert srv.usd(500) == '$5.00'
    assert srv.usd(50_000) == '$500.00'
    assert srv.usd(0) == '$0.00'
    assert srv.usd(1) == '$0.01'


async def test_create_order_sends_cents_verbatim():
    """Регресс на единицы: сколько центов пришло в инструмент — столько ушло в API."""
    rec = use_client(json_response(order_payload(), 201))
    result = await srv.create_order(
        order_type='post', customer_id=CUSTOMER, reward_cents=500,
        subreddit='test', title='Hi', body='text', confirm=True,
    )
    assert rec.json_body()['rewardCents'] == 500
    assert result['order']['rewardCents'] == 500
    assert result['order']['reward'] == '$5.00'


async def test_preview_shows_cents_and_dollars_side_by_side():
    rec = use_client(json_response(order_payload(), 201))
    result = await srv.create_order(
        order_type='post', customer_id=CUSTOMER, reward_cents=500,
        subreddit='test', title='Hi', body='text',
    )
    assert rec.calls == 0  # ни одного запроса до подтверждения
    assert result['created'] is False
    assert result['preview']['rewardCents'] == 500
    assert result['cost']['reward'] == '$5.00'


async def test_dollar_sized_reward_is_rejected_before_the_network():
    rec = use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError) as exc:
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=5_000_000,
            subreddit='test', title='Hi', body='text', confirm=True,
        )
    assert 'reward_cents=500' in str(exc.value)  # подсказка про центы
    assert rec.calls == 0


async def test_suspiciously_high_reward_is_flagged_in_warnings():
    use_client(json_response(order_payload(), 201))
    result = await srv.create_order(
        order_type='post', customer_id=CUSTOMER, reward_cents=50_000,
        subreddit='test', title='Hi', body='text',
    )
    assert any('$500.00' in w for w in result['warnings'])


async def test_boolean_reward_is_rejected():
    use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError):
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=True,
            subreddit='test', title='Hi', body='text', confirm=True,
        )


# --- Подтверждение ----------------------------------------------------------------


async def test_confirmation_is_required_before_spending():
    rec = use_client(json_response(order_payload(), 201))
    result = await srv.create_order(
        order_type='comment', customer_id=CUSTOMER, reward_cents=300,
        target_url='https://reddit.com/r/x/comments/1',
    )
    assert result['status'] == 'CONFIRMATION_REQUIRED'
    assert rec.calls == 0

    created = await srv.create_order(
        order_type='comment', customer_id=CUSTOMER, reward_cents=300,
        target_url='https://reddit.com/r/x/comments/1', confirm=True,
    )
    assert created['status'] == 'CREATED'
    assert rec.calls == 1


async def test_batch_preview_multiplies_cost_by_executors():
    rec = use_client(json_response({'created': 3, 'orderIds': ['a', 'b', 'c']}, 201))
    execs = [EXECUTOR, CUSTOMER, PROJECT]  # три разных UUID
    result = await srv.create_orders_batch(
        order_type='mass_comment', customer_id=CUSTOMER, subreddit='test',
        reward_cents=500, executor_ids=execs, quantity=2,
    )
    assert rec.calls == 0
    assert result['cost']['ordersToCreate'] == 3
    assert result['cost']['rewardPerExecutor'] == '$5.00'
    assert result['cost']['maxTotal'] == '$15.00'


async def test_batch_assign_all_admits_unknown_total():
    use_client(json_response({'created': 0, 'orderIds': []}, 201))
    result = await srv.create_orders_batch(
        order_type='mass_post', customer_id=CUSTOMER, subreddit='test',
        reward_cents=500, assign_all=True,
    )
    assert result['cost']['maxTotal'] == 'unknown'
    assert any('assign_all' in w for w in result['warnings'])


async def test_batch_without_executors_is_rejected():
    rec = use_client(json_response({'created': 0}, 201))
    with pytest.raises(ToolError):
        await srv.create_orders_batch(
            order_type='mass_post', customer_id=CUSTOMER, subreddit='test', reward_cents=100,
        )
    assert rec.calls == 0


# --- Валидация до сети ------------------------------------------------------------


async def test_post_without_title_is_rejected_locally():
    rec = use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError) as exc:
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=100,
            subreddit='test', body='text', confirm=True,
        )
    assert 'title is required' in str(exc.value)
    assert rec.calls == 0


async def test_comment_without_target_url_is_rejected_locally():
    rec = use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError) as exc:
        await srv.create_order(
            order_type='comment', customer_id=CUSTOMER, reward_cents=100, confirm=True,
        )
    assert 'target_url' in str(exc.value)
    assert rec.calls == 0


async def test_post_with_empty_body_is_rejected_locally():
    use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError) as exc:
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=100,
            subreddit='test', title='Hi', confirm=True,
        )
    assert 'body' in str(exc.value)


async def test_quantity_only_for_mass_orders():
    use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError):
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=100,
            subreddit='test', title='Hi', body='t', quantity=5, confirm=True,
        )


async def test_mass_quantity_ceiling():
    use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError):
        await srv.create_order(
            order_type='mass_comment', customer_id=CUSTOMER, reward_cents=100,
            subreddit='test', quantity=99, confirm=True,
        )


async def test_name_instead_of_uuid_is_rejected():
    rec = use_client(json_response(order_payload(), 201))
    with pytest.raises(ToolError) as exc:
        await srv.create_order(
            order_type='post', customer_id='Acme Inc', reward_cents=100,
            subreddit='test', title='Hi', body='t', confirm=True,
        )
    assert 'UUID' in str(exc.value)
    assert rec.calls == 0


async def test_assign_order_rejects_everyone_plus_personal():
    rec = use_client(json_response(order_payload(), 200))
    with pytest.raises(ToolError):
        await srv.assign_order(order_id=ORDER, executor_ids=[EXECUTOR], open_to_everyone=True)
    assert rec.calls == 0


# --- Ретраи и ошибки на уровне инструментов ---------------------------------------


async def test_create_order_never_retries():
    """Мокаем падающий транспорт: запрос обязан уйти ровно один раз."""
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom')

    rec = use_client(handler)
    with pytest.raises(ApiError):
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=500,
            subreddit='test', title='Hi', body='text', confirm=True,
        )
    assert rec.calls == 1


async def test_403_reaches_the_tool_with_a_readable_hint():
    use_client(api_error(403, 'FORBIDDEN', 'Forbidden'))
    with pytest.raises(ApiError) as exc:
        await srv.create_order(
            order_type='post', customer_id=CUSTOMER, reward_cents=500,
            subreddit='test', title='Hi', body='text', confirm=True,
        )
    text = str(exc.value)
    assert '403' in text and 'orders:write' in text
    assert TEST_KEY not in text


async def test_401_reaches_the_tool_and_never_shows_the_key():
    use_client(api_error(401, 'UNAUTHORIZED', f'bad token {TEST_KEY}'))
    with pytest.raises(ApiError) as exc:
        await srv.list_orders()
    text = str(exc.value)
    assert 'revoked' in text
    assert TEST_KEY not in text


async def test_missing_key_surfaces_as_tool_error():
    srv.set_client(None)
    with pytest.raises(ToolError) as exc:
        await srv.list_orders()
    assert 'FEEDHEAT_API_KEY' in str(exc.value)


# --- Чтение -----------------------------------------------------------------------


async def test_list_orders_trims_and_formats_money():
    use_client(json_response([order_payload(), order_payload(id='x', rewardCents=1234)]))
    result = await srv.list_orders(status='open', limit=1)
    assert result['count'] == 1
    assert result['totalMatched'] == 2
    assert result['orders'][0]['reward'] == '$5.00'
    assert 'body' not in result['orders'][0]  # список короткий, тела нет


async def test_list_clients_counts_orders_and_fetches_projects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/admin/customers':
            return httpx.Response(200, json=[{
                'user': {'id': CUSTOMER, 'displayName': 'Acme', 'username': 'acme',
                         'status': 'active'},
                'ordersByStatus': {'open': 2, 'completed': 3},
                'executors': [{'executorId': EXECUTOR}],
                'openToEveryone': False,
            }])
        return httpx.Response(200, json=[{'id': PROJECT, 'name': 'Widget', 'language': 'en'}])

    use_client(handler)
    result = await srv.list_clients()
    row = result['clients'][0]
    assert row['ordersTotal'] == 5
    assert row['projects'][0]['id'] == PROJECT


async def test_list_clients_can_skip_projects():
    rec = use_client(json_response([{
        'user': {'id': CUSTOMER, 'displayName': 'Acme', 'status': 'active'},
        'ordersByStatus': {},
    }]))
    result = await srv.list_clients(include_projects=False)
    assert rec.calls == 1
    assert 'projects' not in result['clients'][0]


async def test_list_executors_hides_inactive_by_default():
    payload = [
        {'user': {'id': EXECUTOR, 'displayName': 'A', 'status': 'active'}, 'activeClaims': 2,
         'earnedCents': 1000},
        {'user': {'id': CUSTOMER, 'displayName': 'B', 'status': 'pending'}, 'activeClaims': 0},
    ]
    use_client(json_response(payload))
    result = await srv.list_executors()
    assert result['count'] == 1
    assert result['executors'][0]['earned'] == '$10.00'


async def test_order_candidates_warns_when_nobody_is_in_scope():
    use_client(json_response({'executorIds': [], 'executors': []}))
    result = await srv.order_candidates(ORDER)
    assert result['count'] == 0
    assert 'cannot be claimed' in result['note']


async def test_open_order_hits_the_open_endpoint():
    rec = use_client(json_response(order_payload(status='open')))
    result = await srv.open_order(ORDER)
    assert rec.last.url.path == f'/api/admin/orders/{ORDER}/open'
    assert result['status'] == 'OPENED'


async def test_cancel_preview_only_reads_and_never_cancels():
    rec = use_client(json_response(order_payload(status='open')))
    result = await srv.cancel_order(ORDER)
    assert result['status'] == 'CONFIRMATION_REQUIRED'
    assert result['cancelled'] is False
    assert result['preview']['reward'] == '$5.00'
    assert rec.calls == 1
    assert rec.last.method == 'GET'
    assert rec.last.url.path == f'/api/orders/{ORDER}'


async def test_cancel_preview_warns_when_an_executor_is_working():
    use_client(json_response(order_payload(
        status='claimed', assignedExecutorIds=[EXECUTOR], assignedExecutorNames=['Bob'],
    )))
    result = await srv.cancel_order(ORDER)
    assert any('loses' in w or 'lose the started work' in w for w in result['warnings'])
    assert any('Bob' in w for w in result['warnings'])


async def test_cancel_preview_warns_on_uncancellable_status():
    use_client(json_response(order_payload(status='completed')))
    result = await srv.cancel_order(ORDER)
    assert any('409' in w for w in result['warnings'])


async def test_cancel_with_confirm_posts_once_to_the_orders_router():
    rec = use_client(json_response(order_payload(status='cancelled')))
    result = await srv.cancel_order(ORDER, confirm=True)
    assert rec.calls == 1
    assert rec.last.method == 'POST'
    assert rec.last.url.path == f'/api/orders/{ORDER}/cancel'  # общий роутер, не /admin
    assert result['cancelled'] is True
    assert result['order']['status'] == 'cancelled'


async def test_cancel_of_completed_order_is_readable():
    use_client(api_error(
        409, 'INVALID_STATUS',
        'Order must be in status draft or pending or open or needs_revision or claimed, '
        'current status is completed',
    ))
    with pytest.raises(ApiError) as exc:
        await srv.cancel_order(ORDER, confirm=True)
    text = str(exc.value)
    assert '409' in text and 'current status is completed' in text
    assert TEST_KEY not in text


async def test_cancel_never_retries():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom')

    rec = use_client(handler)
    with pytest.raises(ApiError):
        await srv.cancel_order(ORDER, confirm=True)
    assert rec.calls == 1


async def test_assign_order_replaces_the_whole_assignment():
    rec = use_client(json_response(order_payload(assignedExecutorIds=[EXECUTOR])))
    await srv.assign_order(order_id=ORDER, executor_ids=[EXECUTOR])
    assert rec.json_body() == {'executorIds': [EXECUTOR], 'openToEveryone': False}
