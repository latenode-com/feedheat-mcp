# MCP-сервер заказчика: свои проекты, свои заказы, статусы и отдача.
#
# Третий сервер в пакете и первый, который работает НЕ от имени площадки.
# Ключ здесь принадлежит заказчику (apps.accounts.models.ApiKey, client:*), и
# сужение до его учётки делает бэкенд — тот же workspace_owner_id, что и для
# живой сессии. Поэтому здесь нет и не должно быть параметра «чей заказ»:
# подставить чужой customerId некуда, клиентская ветка /api/orders его не читает.
#
# Зачем он вообще. Агентство ведёт свою отчётность в таблице, и до сих пор
# единственным способом её наполнить было открыть панель и переписать статусы
# руками. status_table отдаёт те же строки готовым TSV — вставляется в Google
# Sheets как есть.
#
# Что сюда намеренно не попало:
#   • план и обязательства по проекту — их обсуждают с менеджером, и заказчику
#     целевые значения не отдаёт даже UI (projects/schemas.py, project_out);
#   • исполнитель, его ник и ставка — нейтральность площадки, см. order_out;
#   • приёмка работ — ревью только у модератора (orders/api.py, review_order).
#
# Тексты для модели — по-английски, как и в остальных серверах пакета.
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from feedheat_mcp.client import ApiError, ConfigError, CustomerClient

server = MCPServer(
    name='feedheat-client',
    version='0.1.0',
    instructions=(
        'You work on ONE FeedHeat Crowd account: the company that owns the API key. '
        'FeedHeat is a marketplace where people are paid to post and comment on Reddit '
        'for that company. Every tool here returns that account\'s own projects and '
        'orders — there is no way to reach another company\'s data, and no parameter '
        'that would name one.\n\n'
        'Typical work: report on what was published, find what is stuck, and put the '
        'numbers into the account\'s own spreadsheet. status_table exists for exactly '
        'that — it returns TSV that pastes into Google Sheets or Excel unchanged.\n\n'
        'Order statuses mean: draft (created, not paid for yet, nobody can take it), '
        'open (waiting for a writer), claimed/submitted (being worked on), '
        'needs_revision (sent back for a fix), retention (published and under '
        'observation — the content is already live), completed (done and accepted), '
        'cancelled. An order counts as live on Reddit from publishedAt onward, which '
        'includes both retention and completed.\n\n'
        'Never invent a status, a date or a link. Every number you report must come '
        'from a tool result. When something is missing, say it is missing.\n\n'
        'Writing tools spend real money: create_order and publish_order put paid work '
        'in front of real people. Never call them to "test" anything, and never call '
        'publish_order on an order you did not just create or were not asked about by '
        'name. A mistake that has not been taken by anybody yet is fixed with '
        'unpublish_order, which costs nothing. If the key is read-only these tools '
        'fail with 403 — that is a setting, not a problem to route around.'
    ),
)

_client: CustomerClient | None = None


def set_client(client: CustomerClient | None) -> None:
    """Подмена клиента в тестах."""
    global _client
    _client = client


def get_client() -> CustomerClient:
    global _client
    if _client is None:
        try:
            _client = CustomerClient()
        except ConfigError as exc:
            raise ToolError(str(exc)) from None
    return _client


# ── Общее ──────────────────────────────────────────────────────────────────

# Статусы, при которых контент уже на Reddit. Совпадает с PUBLISHED_STATUSES на
# бэкенде и держится здесь отдельной константой намеренно: отчёт агентства
# считает «сделано» именно по ним, и молчаливый разъезд с бэкендом должен
# ломать тест, а не тихо менять цифру в чужой таблице.
PUBLISHED = frozenset({'completed', 'retention'})
IN_WORK = frozenset({'claimed', 'submitted', 'needs_revision'})

# Типы заказов, доступные ЗАКАЗЧИКУ. Массовых (mass_post, mass_comment) здесь
# нет намеренно: их схема принимает только админская ручка, и попытка завести
# такой заказ клиентским ключом отбивается на валидации с невнятным текстом.
ORDER_TYPES = ('comment', 'reply', 'post')
COMMENT_LIKE = ('comment', 'reply')


async def _call(coro) -> Any:
    try:
        return await coro
    except ApiError as exc:
        raise ToolError(str(exc)) from None


def _day(value: str | None, field: str) -> date | None:
    """YYYY-MM-DD → date. Молча игнорировать мусор нельзя: отчёт за 'сентябрь',
    посчитанный по всей истории, выглядит как правдоподобная цифра."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(
            f'{field} must be a date like 2026-09-01, got {value!r}.'
        ) from None


def _at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


async def _project_names() -> dict[str, str]:
    """id проекта → его имя.

    Нужна потому, что клиенту список заказов приходит компактным: в нём есть
    projectId, а брифа проекта нет вовсе (orders/api.py, compact=True). Без
    этой карты колонка «проект» в отчёте и фильтр по проекту оставались бы
    пустыми — то есть главное, ради чего агентство и берёт выгрузку.
    """
    data = await _call(get_client().projects())
    return {str(p.get('id')): (p.get('name') or '') for p in data or []}


def _row(order: dict, names: dict[str, str] | None = None) -> dict:
    """Одна строка отчёта. Собирается здесь, а не на стороне модели: иначе
    каждая выгрузка получалась бы со своим набором колонок."""
    metrics = order.get('metrics') or {}
    published = _at(order.get('publishedAt'))
    project = order.get('project') or {}
    project_id = str(order.get('projectId') or '')
    return {
        'orderId': order.get('id'),
        'projectId': project_id,
        'project': project.get('name') or (names or {}).get(project_id, ''),
        'type': order.get('type') or '',
        'status': order.get('status') or '',
        'live': (order.get('status') in PUBLISHED),
        'subreddit': order.get('subreddit') or '',
        'title': order.get('title') or '',
        'targetUrl': order.get('targetUrl') or '',
        'quantity': order.get('quantity') or 1,
        'createdAt': order.get('createdAt'),
        'publishedAt': order.get('publishedAt'),
        'publishedOn': published.date().isoformat() if published else '',
        'retentionUntil': order.get('retentionUntil'),
        'resultUrls': list(order.get('resultUrls') or []),
        'postedFrom': order.get('resultAccount') or '',
        'upvotes': metrics.get('score'),
        'comments': metrics.get('numComments'),
        'metricsAt': metrics.get('capturedAt'),
        'editRequested': bool(order.get('editRequested')),
        'republishing': bool(order.get('republishing')),
    }


def _match(row: dict, *, project: str | None, since: date | None,
           until: date | None, date_field: str) -> bool:
    if project and project.lower() not in (row['project'] or '').lower():
        return False
    if since or until:
        stamp = _at(row.get(date_field))
        if stamp is None:
            # Нет даты по выбранному полю — строка в окно не попадает. Для
            # date_field='publishedAt' это и значит «ещё не опубликовано»:
            # тянуть такую строку в отчёт за период было бы враньём.
            return False
        day = stamp.date()
        if since and day < since:
            return False
        if until and day > until:
            return False
    return True


async def _rows(*, status: str | None, project: str | None, since: str | None,
                until: str | None, date_field: str) -> list[dict]:
    if date_field not in ('createdAt', 'publishedAt'):
        raise ToolError("date_field must be 'createdAt' or 'publishedAt'.")
    a, b = _day(since, 'since'), _day(until, 'until')
    if a and b and a > b:
        raise ToolError(f'since ({since}) is after until ({until}).')
    orders = await _call(get_client().orders(status=status))
    names = await _project_names()
    rows = [_row(o, names) for o in orders or []]
    return [r for r in rows
            if _match(r, project=project, since=a, until=b, date_field=date_field)]


# ── Чтение ─────────────────────────────────────────────────────────────────

@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def projects() -> list[dict]:
    """The projects on this account: what each one promotes and where.

    Start here when a request names a product or a brand rather than an order id —
    order listings carry the project name, and this is where you learn which names
    exist. targetSubreddits is where that project is meant to be posted.

    Monthly targets and commitments are deliberately not part of this: they live
    with your FeedHeat manager, not in the API.
    """
    data = await _call(get_client().projects())
    return [
        {
            'id': p.get('id'),
            'name': p.get('name'),
            'brand': p.get('brandName') or '',
            'productUrl': p.get('productUrl') or '',
            'language': p.get('language') or 'en',
            'targetSubreddits': list(p.get('targetSubreddits') or []),
            'createdAt': p.get('createdAt'),
        }
        for p in data or []
    ]


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def orders(status: str | None = None, project: str | None = None,
                 since: str | None = None, until: str | None = None,
                 date_field: str = 'createdAt') -> dict:
    """Your orders with their current status, publication links and Reddit numbers.

    This is the working list: one entry per order, already flattened, so you can
    answer "what is live", "what is stuck", "what did we publish in September"
    without further calls.

    status filters server-side (draft, open, claimed, submitted, needs_revision,
    retention, completed, cancelled). project matches the project name, partially
    and case-insensitively.

    since and until are YYYY-MM-DD, inclusive, and apply to date_field. Use
    'publishedAt' for reporting on work delivered in a period — orders that are
    not published yet then drop out, which is the point. Use 'createdAt' to see
    everything ordered in a period regardless of how far it got.

    The counts in the summary use the same rule the platform does: live means
    completed or retention, because retention content is already on Reddit.
    """
    rows = await _rows(status=status, project=project, since=since, until=until,
                       date_field=date_field)
    live = [r for r in rows if r['live']]
    return {
        'summary': {
            'orders': len(rows),
            'live': len(live),
            'inWork': sum(1 for r in rows if r['status'] in IN_WORK),
            'open': sum(1 for r in rows if r['status'] == 'open'),
            'draft': sum(1 for r in rows if r['status'] == 'draft'),
            'upvotes': sum(r['upvotes'] or 0 for r in live),
            'comments': sum(r['comments'] or 0 for r in live),
        },
        'orders': rows,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def status_table(project: str | None = None, since: str | None = None,
                       until: str | None = None, status: str | None = None,
                       date_field: str = 'createdAt', delimiter: str = 'tab') -> str:
    """The same orders as a table, ready to paste into a spreadsheet.

    Returns text: a header row and one line per order, tab-separated by default
    (delimiter='comma' gives CSV). Paste it straight into Google Sheets or Excel —
    no reformatting, no manual retyping of statuses.

    Filters behave exactly as in `orders`. For a monthly report to a client, the
    usual call is date_field='publishedAt' with since and until set to that month.

    Multiple publication links on one order are joined by a newline inside the cell,
    which is what spreadsheets expect.
    """
    rows = await _rows(status=status, project=project, since=since, until=until,
                       date_field=date_field)
    sep = ',' if delimiter == 'comma' else '\t'
    if delimiter not in ('tab', 'comma'):
        raise ToolError("delimiter must be 'tab' or 'comma'.")
    columns = [
        ('Project', 'project'), ('Type', 'type'), ('Status', 'status'),
        ('Subreddit', 'subreddit'), ('Title', 'title'), ('Target URL', 'targetUrl'),
        ('Quantity', 'quantity'), ('Created', 'createdAt'),
        ('Published', 'publishedOn'), ('Retention until', 'retentionUntil'),
        ('Links', 'resultUrls'), ('Posted from', 'postedFrom'),
        ('Upvotes', 'upvotes'), ('Comments', 'comments'), ('Order id', 'orderId'),
    ]
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=sep, lineterminator='\n',
                        quoting=csv.QUOTE_MINIMAL)
    writer.writerow([title for title, _ in columns])
    for row in rows:
        writer.writerow([
            '\n'.join(row[key]) if key == 'resultUrls'
            else ('' if row[key] is None else row[key])
            for _, key in columns
        ])
    return buf.getvalue()


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def order(order_id: str) -> dict:
    """One order in full: the brief that was ordered, where it went, what it scored.

    Use it when a listing is not enough — the text of the task, the attempt history
    the platform shows you, and every accepted publication link.

    Who wrote it is not here and will not be: writers are anonymous to clients by
    design, on every screen and in every export.
    """
    if not (order_id or '').strip():
        raise ToolError('order_id is empty.')
    data = await _call(get_client().order(order_id.strip()))
    row = _row(data, await _project_names())
    row['body'] = data.get('body') or ''
    row['instructions'] = data.get('instructions') or ''
    row['attempts'] = data.get('attempts') or []
    return row


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def results(days: int = 30, project_id: str | None = None) -> dict:
    """Upvotes and comments your published content collected over the last N days.

    The platform's own counting, not a sum of the table: it reads the latest
    successful snapshot per post. Use it for "what did this month give us" and take
    per-order numbers from `orders` or `status_table`.
    """
    if days < 1 or days > 730:
        raise ToolError('days must be between 1 and 730.')
    return await _call(get_client().metrics_summary(days=days, project_id=project_id))


# ── Запись ─────────────────────────────────────────────────────────────────

@server.tool(annotations=ToolAnnotations(read_only_hint=False,
                                         destructive_hint=False, open_world_hint=True))
async def create_order(project_id: str, type: str, subreddit: str = '',
                       target_url: str = '', title: str = '', body: str = '',
                       instructions: str = '', quantity: int = 1,
                       publish: bool = False) -> dict:
    """Create one order. It starts as a draft and costs nothing until it is published.

    type is one of comment, reply or post. A comment and a reply need target_url —
    the Reddit thread to answer. A post needs subreddit and title.

    body is the finished text to publish. Leave it empty and put the task in
    instructions instead when you want the writer to compose it.

    publish=True sends it on for review straight away, which is the moment the
    money is committed. Pass it only when you were asked to publish, not to
    "save time". Otherwise create the draft and let a person look at it.

    The rate paid to the writer comes from your agreement with FeedHeat and is
    applied automatically — there is no price field here and nothing to set.
    """
    kind = (type or '').strip()
    if kind not in ORDER_TYPES:
        raise ToolError(f'type must be one of: {", ".join(ORDER_TYPES)}.')
    if not (project_id or '').strip():
        raise ToolError('project_id is required — call projects() to get it.')
    if kind in COMMENT_LIKE and not (target_url or '').strip():
        raise ToolError('A comment needs target_url: the Reddit thread to answer.')
    if kind == 'post' and not (subreddit or '').strip():
        raise ToolError('A post needs subreddit.')
    if not (body or '').strip() and not (instructions or '').strip():
        raise ToolError(
            'Either body (the finished text) or instructions (what to write) must be '
            'filled in. An order with neither cannot be worked on.'
        )
    payload = {
        'projectId': project_id.strip(), 'type': kind,
        'subreddit': (subreddit or '').strip() or None,
        'targetUrl': (target_url or '').strip() or None,
        'title': (title or '').strip() or None,
        'body': body or '', 'instructions': instructions or '',
        'quantity': max(1, int(quantity or 1)),
    }
    created = await _call(get_client().create_order(payload))
    if publish:
        created = await _call(get_client().publish_order(str(created.get('id'))))
    return _row(created)


@server.tool(annotations=ToolAnnotations(read_only_hint=False,
                                         destructive_hint=False, open_world_hint=True))
async def publish_order(order_id: str) -> dict:
    """Send a draft out to be worked on. This is what commits the money.

    The order does not go straight to writers: it lands in review first (status
    becomes pending), and a FeedHeat moderator opens it to the pool. So "published"
    here means "submitted", and the same day it usually becomes open.

    Only draft and needs_revision orders can be sent; anything further along
    fails with 409. Use unpublish_order to pull it back while nobody has taken it.
    """
    if not (order_id or '').strip():
        raise ToolError('order_id is empty.')
    return _row(await _call(get_client().publish_order(order_id.strip())))


@server.tool(annotations=ToolAnnotations(read_only_hint=False,
                                         destructive_hint=False, open_world_hint=True))
async def unpublish_order(order_id: str) -> dict:
    """Pull an order back into drafts while nobody is working on it.

    The counterpart to publish_order, and the right fix for a wrong link, a wrong
    subreddit or a duplicate: the order returns to draft, keeps its text, and can
    be corrected and sent again. Nothing is lost and nobody is charged.

    Works from pending (submitted, waiting for review) and open (waiting for a
    writer). Once somebody has taken it, this fails with 409 — at that point a
    real person is already writing, and the honest option is cancel_order.
    """
    if not (order_id or '').strip():
        raise ToolError('order_id is empty.')
    return _row(await _call(get_client().unpublish_order(order_id.strip())))


@server.tool(annotations=ToolAnnotations(read_only_hint=False,
                                         destructive_hint=True, open_world_hint=True))
async def cancel_order(order_id: str) -> dict:
    """Cancel an order. Use it to undo your own mistake — a wrong subreddit, a
    duplicate, a thread that no longer exists.

    A writer who had already taken it is not penalised for the cancellation.
    """
    if not (order_id or '').strip():
        raise ToolError('order_id is empty.')
    return _row(await _call(get_client().cancel_order(order_id.strip())))


@server.tool(annotations=ToolAnnotations(read_only_hint=False,
                                         destructive_hint=False, open_world_hint=True))
async def update_order(order_id: str, title: str | None = None, body: str | None = None,
                       instructions: str | None = None, subreddit: str | None = None,
                       target_url: str | None = None) -> dict:
    """Edit an order that has not been worked on yet. Only the fields you pass change.

    Once somebody is writing it, the platform refuses the edit (409) — changing the
    task under a person who already started it is the one thing this must not do.
    """
    if not (order_id or '').strip():
        raise ToolError('order_id is empty.')
    patch = {k: v for k, v in {
        'title': title, 'body': body, 'instructions': instructions,
        'subreddit': subreddit, 'targetUrl': target_url,
    }.items() if v is not None}
    if not patch:
        raise ToolError('Nothing to change — pass at least one field.')
    return _row(await _call(get_client().update_order(order_id.strip(), patch)))


def main() -> None:
    server.run()


if __name__ == '__main__':
    main()
