# Клиентский сервер: чужую учётку назвать нечем, таблица собирается здесь,
# трата денег требует отдельного скоупа.
from __future__ import annotations

import csv
import inspect
import io

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from feedheat_mcp import client_server as cli
from tests.conftest import api_error, json_response, make_customer_client

PROJECT = '11111111-1111-1111-1111-111111111111'
ORDER = '22222222-2222-2222-2222-222222222222'


def use_client(handler):
    client, rec = make_customer_client(handler)
    cli.set_client(client)
    return rec


def routed(orders, projects=None):
    """Разный ответ на /api/orders/ и /api/projects/ — как у живого бэкенда."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/projects/':
            return httpx.Response(200, json=projects if projects is not None else [])
        return httpx.Response(200, json=orders)

    return handler


def an_order(**over):
    base = {
        'id': ORDER, 'type': 'comment', 'status': 'completed',
        'subreddit': 'VPS', 'title': '', 'targetUrl': 'https://reddit.com/r/VPS/x',
        'quantity': 1, 'createdAt': '2026-09-01T10:00:00+00:00',
        'publishedAt': '2026-09-03T12:00:00+00:00', 'retentionUntil': None,
        'resultUrls': ['https://reddit.com/r/VPS/x/c1'], 'resultAccount': 'someone',
        'project': {'name': 'WinSpirit'},
        'metrics': {'score': 12, 'numComments': 3, 'capturedAt': '2026-09-05T00:00:00+00:00'},
    }
    base.update(over)
    return base


# --- Чужую учётку назвать нечем --------------------------------------------

def test_no_tool_accepts_a_customer_id():
    """Главная гарантия сервера: параметра «чей заказ» нет ни у одного инструмента.

    Сужение делает бэкенд по владельцу ключа. Если такой параметр когда-нибудь
    появится, эта проверка должна упасть раньше, чем ключ уедет агентству.
    """
    tools = (cli.projects, cli.orders, cli.status_table, cli.order, cli.results,
             cli.create_order, cli.publish_order, cli.cancel_order, cli.update_order)
    banned = {'customer_id', 'customerId', 'customer', 'owner_id', 'user_id'}
    for tool in tools:
        assert not (set(inspect.signature(tool).parameters) & banned), tool


async def test_reads_hit_client_endpoints():
    rec = use_client(routed([]))
    await cli.projects()
    assert rec.last.url.path == '/api/projects/'
    await cli.orders()
    # orders() ходит за списком заказов и за именами проектов — второе нужно,
    # чтобы колонка «проект» не была пустой (см. _project_names).
    paths = {r.url.path for r in rec.requests}
    assert paths == {'/api/projects/', '/api/orders/'}


# --- Статусы и счёт ---------------------------------------------------------

async def test_live_counts_retention_as_published():
    """retention — это уже опубликованный контент, и в отчёт он идёт как сделанный."""
    use_client(json_response([
        an_order(status='completed'),
        an_order(status='retention', metrics={'score': 5, 'numComments': 1}),
        an_order(status='open', publishedAt=None, metrics=None),
        an_order(status='claimed', publishedAt=None, metrics=None),
        an_order(status='draft', publishedAt=None, metrics=None),
    ]))
    out = await cli.orders()
    assert out['summary'] == {
        'orders': 5, 'live': 2, 'inWork': 1, 'open': 1, 'draft': 1,
        'upvotes': 17, 'comments': 4,
    }


async def test_status_filter_goes_to_the_server():
    rec = use_client(routed([]))
    await cli.orders(status='open')
    orders_call = next(r for r in rec.requests if r.url.path == '/api/orders/')
    assert dict(orders_call.url.params) == {'status': 'open'}


async def test_project_name_comes_from_the_projects_list():
    """Клиенту заказ приходит без брифа проекта — только projectId. Имя
    подставляем сами, иначе колонка «проект» в отчёте пустая."""
    use_client(routed(
        [an_order(project=None, projectId=PROJECT)],
        projects=[{'id': PROJECT, 'name': 'WinSpirit'}],
    ))
    out = await cli.orders()
    assert out['orders'][0]['project'] == 'WinSpirit'
    assert out['orders'][0]['projectId'] == PROJECT


async def test_project_filter_works_without_the_embedded_brief():
    use_client(routed(
        [an_order(project=None, projectId=PROJECT),
         an_order(project=None, projectId='other-id')],
        projects=[{'id': PROJECT, 'name': 'WinSpirit'},
                  {'id': 'other-id', 'name': 'Revenue Grid'}],
    ))
    out = await cli.orders(project='winspirit')
    assert [r['project'] for r in out['orders']] == ['WinSpirit']


async def test_project_filter_is_partial_and_case_insensitive():
    use_client(json_response([
        an_order(project={'name': 'WinSpirit'}),
        an_order(project={'name': 'Revenue Grid'}),
    ]))
    out = await cli.orders(project='winspirit')
    assert [r['project'] for r in out['orders']] == ['WinSpirit']


# --- Окно дат ---------------------------------------------------------------

async def test_published_window_drops_what_is_not_published_yet():
    """Отчёт «что сделано за сентябрь» не должен считать незавершённое."""
    use_client(json_response([
        an_order(publishedAt='2026-09-03T12:00:00+00:00'),
        an_order(publishedAt='2026-08-30T12:00:00+00:00'),
        an_order(status='open', publishedAt=None),
    ]))
    out = await cli.orders(since='2026-09-01', until='2026-09-30',
                           date_field='publishedAt')
    assert out['summary']['orders'] == 1


async def test_created_window_keeps_unpublished():
    use_client(json_response([
        an_order(createdAt='2026-09-01T10:00:00+00:00', status='open', publishedAt=None),
        an_order(createdAt='2026-07-01T10:00:00+00:00'),
    ]))
    out = await cli.orders(since='2026-09-01', until='2026-09-30')
    assert out['summary']['orders'] == 1


async def test_bad_date_is_refused_not_ignored():
    """Молча проигнорированный фильтр даёт правдоподобную, но чужую цифру."""
    use_client(json_response([]))
    with pytest.raises(ToolError) as e:
        await cli.orders(since='сентябрь')
    assert '2026-09-01' in str(e.value)


async def test_reversed_window_is_refused():
    use_client(json_response([]))
    with pytest.raises(ToolError):
        await cli.orders(since='2026-09-30', until='2026-09-01')


async def test_unknown_date_field_is_refused():
    use_client(json_response([]))
    with pytest.raises(ToolError):
        await cli.orders(date_field='updatedAt')


# --- Таблица ----------------------------------------------------------------

async def test_status_table_is_tsv_with_header():
    use_client(json_response([an_order()]))
    text = await cli.status_table()
    rows = list(csv.reader(io.StringIO(text), delimiter='\t'))
    assert rows[0][:4] == ['Project', 'Type', 'Status', 'Subreddit']
    assert rows[1][0] == 'WinSpirit'
    assert rows[1][-1] == ORDER


async def test_status_table_joins_several_links_in_one_cell():
    use_client(json_response([an_order(resultUrls=['https://a/1', 'https://a/2'])]))
    text = await cli.status_table()
    rows = list(csv.reader(io.StringIO(text), delimiter='\t'))
    links = rows[1][10]
    assert links == 'https://a/1\nhttps://a/2'


async def test_status_table_comma_variant():
    use_client(json_response([an_order()]))
    text = await cli.status_table(delimiter='comma')
    assert text.splitlines()[0].startswith('Project,Type,Status')


async def test_status_table_empty_period_is_header_only():
    use_client(json_response([an_order(publishedAt='2026-08-01T00:00:00+00:00')]))
    text = await cli.status_table(since='2026-09-01', date_field='publishedAt')
    assert len(text.strip().splitlines()) == 1


# --- Запись -----------------------------------------------------------------

async def test_comment_without_target_url_is_refused_before_the_call():
    rec = use_client(json_response({}))
    with pytest.raises(ToolError) as e:
        await cli.create_order(project_id=PROJECT, type='comment', body='hi')
    assert 'target_url' in str(e.value)
    assert rec.calls == 0


async def test_post_without_subreddit_is_refused():
    use_client(json_response({}))
    with pytest.raises(ToolError):
        await cli.create_order(project_id=PROJECT, type='post', title='t', body='b')


async def test_order_without_text_or_brief_is_refused():
    """Заказ, по которому нечего писать, до исполнителя доходить не должен."""
    use_client(json_response({}))
    with pytest.raises(ToolError) as e:
        await cli.create_order(project_id=PROJECT, type='comment',
                               target_url='https://reddit.com/x')
    assert 'instructions' in str(e.value)


async def test_unknown_type_is_refused():
    use_client(json_response({}))
    with pytest.raises(ToolError) as e:
        await cli.create_order(project_id=PROJECT, type='upvote',
                               target_url='https://reddit.com/x', body='b')
    assert 'comment' in str(e.value)


async def test_create_stays_a_draft_unless_publish_is_asked():
    rec = use_client(json_response(an_order(status='draft', publishedAt=None)))
    await cli.create_order(project_id=PROJECT, type='comment',
                           target_url='https://reddit.com/x', body='b')
    assert [r.url.path for r in rec.requests] == ['/api/orders/']


async def test_publish_flag_opens_the_order_after_creating_it():
    rec = use_client(json_response(an_order(status='open', publishedAt=None)))
    await cli.create_order(project_id=PROJECT, type='comment',
                           target_url='https://reddit.com/x', body='b', publish=True)
    assert [r.url.path for r in rec.requests] == [
        '/api/orders/', f'/api/orders/{ORDER}/publish',
    ]


async def test_unpublish_goes_to_the_customer_endpoint():
    rec = use_client(json_response(an_order(status='draft', publishedAt=None)))
    await cli.unpublish_order(ORDER)
    assert rec.last.url.path == f'/api/orders/{ORDER}/unpublish'
    assert rec.last.method == 'POST'


async def test_unpublish_of_a_taken_order_surfaces_the_conflict():
    """409 — это «за заказом уже сидит человек», и обходить его нечем."""
    use_client(api_error(409, 'ORDER_NOT_OPEN', 'Order is not open'))
    with pytest.raises(ToolError) as e:
        await cli.unpublish_order(ORDER)
    assert 'ORDER_NOT_OPEN' in str(e.value)


async def test_create_has_no_price_field():
    """Ставку задаёт договор с площадкой, а не модель: поля цены здесь нет."""
    params = set(inspect.signature(cli.create_order).parameters)
    assert not (params & {'reward', 'reward_cents', 'price', 'budget'})


async def test_update_without_fields_is_refused():
    rec = use_client(json_response({}))
    with pytest.raises(ToolError):
        await cli.update_order(ORDER)
    assert rec.calls == 0


async def test_update_sends_only_what_changed():
    rec = use_client(json_response(an_order()))
    await cli.update_order(ORDER, title='new')
    assert rec.json_body() == {'title': 'new'}
    assert rec.last.method == 'PATCH'


# --- Ключ только на чтение --------------------------------------------------

async def test_read_only_key_gets_a_clear_refusal_on_writes():
    """403 по скоупу — это настройка ключа, а не повод пробовать другой путь."""
    use_client(api_error(403, 'SCOPE_DENIED',
                         'API key lacks required scope: client:write'))
    with pytest.raises(ToolError) as e:
        await cli.publish_order(ORDER)
    text = str(e.value)
    assert 'client:write' in text
    assert 'Do not retry' in text


async def test_key_is_never_echoed_in_errors():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text='boom fhk_secret_test_key_do_not_leak')

    use_client(handler)
    with pytest.raises(ToolError) as e:
        await cli.projects()
    assert 'fhk_secret_test_key_do_not_leak' not in str(e.value)


async def test_results_window_is_bounded():
    use_client(json_response({}))
    with pytest.raises(ToolError):
        await cli.results(days=0)
    with pytest.raises(ToolError):
        await cli.results(days=1000)
