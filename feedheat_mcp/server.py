# MCP-сервер администратора FeedHeat Crowd: чтение справочников и создание заказов
# исполнителям от имени реального админского аккаунта (по API-ключу fhk_...).
#
# Почему инструменты, а не «дай мне HTTP» одной ручкой: модель должна видеть единицы
# измерения, обязательные поля по типу заказа и цену ошибки прямо в описании, иначе
# reward_cents уезжает в сто раз (центы против долларов) — это реальные деньги.
#
# Тексты, которые читает модель (docstring'и, ошибки, подсказки), — по-английски:
# так же, как message в config/errors.py на бэкенде. Комментарии — по-русски.
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from feedheat_mcp.client import AdminClient, ApiError, ConfigError

OrderType = Literal['post', 'comment', 'reply', 'mass_comment', 'mass_post']
BodyMode = Literal['text', 'brief']
OrderStatus = Literal[
    'draft', 'pending', 'open', 'claimed', 'submitted', 'needs_revision',
    'retention', 'completed', 'cancelled',
]

# Потолок как в PATCH /admin/orders/{id}/budget на бэкенде — ловим опечатку до сети.
MAX_REWARD_CENTS = 1_000_000
# Выше этого предупреждаем: типовая ставка за задание — единицы долларов, а $200+
# обычно означает, что доллары приняли за центы.
HIGH_REWARD_CENTS = 20_000
MAX_QUANTITY = 20

server = MCPServer(
    name='feedheat-admin',
    version='0.1.0',
    instructions=(
        'Admin access to FeedHeat Crowd (Reddit promotion marketplace) on behalf of a real '
        'admin account. Read tools are free to call. Write tools create real, paid work for '
        'real people: always resolve ids with the read tools first, and never invent a UUID. '
        'All money is in CENTS (reward_cents=500 means $5.00).'
    ),
)

_client: AdminClient | None = None


def set_client(client: AdminClient | None) -> None:
    """Подмена клиента (тесты). В проде клиент создаётся лениво из окружения."""
    global _client
    _client = client


def get_client() -> AdminClient:
    # Ленивая инициализация: без ключа сервер обязан подняться и объяснить проблему
    # в ответе на вызов инструмента, а не падать на старте до MCP-хендшейка.
    global _client
    if _client is None:
        try:
            _client = AdminClient()
        except ConfigError as exc:
            raise ToolError(str(exc)) from None
    return _client


# --- Форматирование --------------------------------------------------------------


def usd(cents: int | None) -> str | None:
    """Центы → '$5.00'. Дублируем деньги строкой, чтобы единицы нельзя было спутать."""
    if cents is None:
        return None
    return f'${cents / 100:.2f}'


def _uuid_str(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError):
        raise ToolError(
            f'{field} must be a UUID taken from list_clients / list_projects / list_executors / '
            f'list_orders, not a name or a guess. Got: {value!r}'
        ) from None


async def _gather_limited(factories: list, limit: int = 5) -> list:
    """Параллельные запросы с потолком — чтобы N клиентов не превратились в N-волновой флуд."""
    sem = asyncio.Semaphore(limit)

    async def run(factory):
        async with sem:
            return await factory()

    return await asyncio.gather(*(run(f) for f in factories))


def _order_brief(o: dict) -> dict:
    """Короткая карточка заказа для списков — без body/brief и истории assignment'ов."""
    reward = o.get('rewardCents')
    return {
        'id': o.get('id'),
        'type': o.get('type'),
        'status': o.get('status'),
        'subreddit': o.get('subreddit'),
        'title': o.get('title'),
        'quantity': o.get('quantity', 1),
        'rewardCents': reward,
        'reward': usd(reward),
        'customerId': o.get('customerId'),
        'customerName': o.get('customerName'),
        'projectId': o.get('projectId'),
        'assignedExecutorIds': o.get('assignedExecutorIds') or [],
        'assignedExecutorNames': o.get('assignedExecutorNames') or [],
        'openToEveryone': o.get('openToEveryone', False),
        'createdAt': o.get('createdAt'),
    }


def _order_full(o: dict) -> dict:
    """Полная карточка: brief + тексты + результат. Проектный бриф вырезаем (шумно)."""
    data = _order_brief(o)
    data.update({
        'targetUrl': o.get('targetUrl'),
        'body': o.get('body'),
        'bodyMode': o.get('bodyMode'),
        'instructions': o.get('instructions'),
        'payoutHoldDays': o.get('payoutHoldDays'),
        'executorFeedback': o.get('executorFeedback'),
        'resultUrls': o.get('resultUrls') or [],
        'resultAccount': o.get('resultAccount'),
        'metrics': o.get('metrics'),
        'updatedAt': o.get('updatedAt'),
        'projectName': (o.get('project') or {}).get('name'),
    })
    return data


# --- Валидация до сети ------------------------------------------------------------


def _check_reward(reward_cents: int) -> None:
    # bool — подкласс int, и True молча стал бы 1 центом
    if isinstance(reward_cents, bool) or not isinstance(reward_cents, int):
        raise ToolError('reward_cents must be a whole number of CENTS (500 = $5.00).')
    if reward_cents < 0:
        raise ToolError('reward_cents cannot be negative.')
    if reward_cents > MAX_REWARD_CENTS:
        raise ToolError(
            f'reward_cents={reward_cents} is {usd(reward_cents)} for one order — above the '
            f'{usd(MAX_REWARD_CENTS)} ceiling. If you meant dollars, multiply by 100 the other '
            'way: $5.00 is reward_cents=500.'
        )


def _resolve_body_mode(order_type: str, body_mode: str | None) -> str:
    """Повтор apps/orders/services.resolve_body_mode — чтобы предупредить до POST."""
    if order_type == 'post':
        return 'text'
    if body_mode in ('text', 'brief'):
        return body_mode
    return 'brief'


def _validate_shape(
    order_type: str, subreddit: str | None, target_url: str | None, title: str | None,
    body: str, body_mode: str | None,
) -> None:
    """Повтор apps/orders/services.validate_order_shape: экономит round-trip и объясняет точнее."""
    if order_type in ('mass_comment', 'mass_post'):
        if not (subreddit or '').strip():
            raise ToolError(f'subreddit is required for {order_type} orders.')
    elif order_type == 'post':
        if not (subreddit or '').strip():
            raise ToolError('subreddit is required for post orders.')
        if not (title or '').strip():
            raise ToolError('title is required for post orders.')
    elif not (target_url or '').strip():
        raise ToolError(
            f'target_url (link to the Reddit post/comment to answer) is required for '
            f'{order_type} orders.'
        )
    if _resolve_body_mode(order_type, body_mode) == 'text' and not (body or '').strip():
        raise ToolError(
            'body must contain the ready-to-post text when body_mode="text" '
            '(post orders are always "text"). Use body_mode="brief" to let the executor write it.'
        )


def _reward_warnings(reward_cents: int, *, per_what: str) -> list[str]:
    out: list[str] = []
    if reward_cents == 0:
        out.append(
            'reward_cents=0 — the executor will see an unpaid task. Set a budget now or later '
            'in the admin panel.'
        )
    elif reward_cents >= HIGH_REWARD_CENTS:
        out.append(
            f'reward_cents={reward_cents} means {usd(reward_cents)} {per_what}. If you meant '
            f'{usd(reward_cents / 100)}, pass reward_cents={reward_cents // 100}.'
        )
    return out


# --- Чтение -----------------------------------------------------------------------


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def list_clients(include_projects: bool = True) -> dict:
    """List FeedHeat customers (clients) with their projects and order counts.

    Use this first: create_order needs a customer_id, and almost always a project_id
    (the project carries the product brief). Both are UUIDs returned here.

    include_projects=True costs one extra request per client; set it to False when you
    only need the list of clients or there are many of them.

    Does NOT return money the client paid to FeedHeat, contracts, or contact history.
    """
    client = get_client()
    rows = await client.get_customers()

    clients = []
    for row in rows:
        user = row.get('user') or {}
        by_status = row.get('ordersByStatus') or {}
        clients.append({
            'id': user.get('id'),
            'name': user.get('displayName'),
            'username': user.get('username'),
            'status': user.get('status'),
            'ordersTotal': sum(int(v or 0) for v in by_status.values()),
            'ordersByStatus': by_status,
            'openToEveryone': row.get('openToEveryone', False),
            'grantedExecutors': len(row.get('executors') or []),
        })

    if include_projects and clients:
        ids = [c['id'] for c in clients if c.get('id')]
        results = await _gather_limited(
            [lambda cid=cid: client.get_customer_projects(cid) for cid in ids]
        )
        by_customer = dict(zip(ids, results, strict=True))
        for c in clients:
            c['projects'] = [
                {
                    'id': p.get('id'),
                    'name': p.get('name'),
                    'productUrl': p.get('productUrl'),
                    'targetSubreddits': p.get('targetSubreddits') or [],
                    'language': p.get('language'),
                }
                for p in (by_customer.get(c['id']) or [])
            ]

    return {'count': len(clients), 'clients': clients}


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def list_projects(customer_id: str) -> dict:
    """List the projects of one customer, with the full brief of each project.

    A project is the product being promoted: description, tone, target subreddits,
    keywords, competitors, things to avoid, call to action, language. Read it before
    writing order body/instructions so the task matches the client's brief.

    customer_id is the UUID from list_clients — not a name.
    """
    cid = _uuid_str(customer_id, 'customer_id')
    rows = await get_client().get_customer_projects(cid)
    return {
        'customerId': cid,
        'count': len(rows),
        'projects': [
            {
                'id': p.get('id'),
                'name': p.get('name'),
                'productUrl': p.get('productUrl'),
                'description': p.get('description'),
                'tone': p.get('tone'),
                'targetSubreddits': p.get('targetSubreddits') or [],
                'targetAudience': p.get('targetAudience'),
                'keywords': p.get('keywords') or [],
                'competitors': p.get('competitors') or [],
                'avoid': p.get('avoid'),
                'callToAction': p.get('callToAction'),
                'language': p.get('language'),
                'openToEveryone': p.get('openToEveryone', False),
            }
            for p in rows
        ],
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def list_executors(include_inactive: bool = False) -> dict:
    """List executors (the people who do the Reddit work) with their current load.

    Returns id, name, how many tasks are in progress (activeClaims), approved/rejected
    counts and earnings. Use the id for create_order(assigned_executor_id=...) or
    assign_order().

    Only executors with status "active" can be assigned; inactive ones are hidden
    unless include_inactive=True.

    Does NOT say who is allowed to see a particular order — the access cascade is not
    visible here. Use order_candidates(order_id) for that.
    """
    client = get_client()
    rows = await client.get_executors()

    people = []
    for row in rows:
        user = row.get('user') or {}
        if not include_inactive and user.get('status') != 'active':
            continue
        people.append({
            'id': user.get('id'),
            'name': user.get('displayName'),
            'username': user.get('username'),
            'status': user.get('status'),
            'activeClaims': row.get('activeClaims', 0),
            'approved': row.get('approved', 0),
            'rejected': row.get('rejected', 0),
            'earnedCents': row.get('earnedCents', 0),
            'earned': usd(row.get('earnedCents')),
        })

    return {'count': len(people), 'executors': people}


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def list_orders(
    status: OrderStatus | None = None,
    customer_id: str | None = None,
    limit: int = 50,
) -> dict:
    """List orders (newest first), optionally filtered by status and/or customer.

    Statuses: draft (created but not in the pool), pending (waiting for moderation),
    open (in the pool / assigned, claimable), claimed (an executor is working on it),
    submitted (proof is waiting for review), needs_revision, retention, completed,
    cancelled.

    Returns short cards without body text — use get_order(order_id) for the full one.
    Call this after a write to confirm what was actually created.
    """
    if limit < 1:
        raise ToolError('limit must be >= 1.')
    cid = _uuid_str(customer_id, 'customer_id') if customer_id else None
    rows = await get_client().get_orders(status=status, customer_id=cid)
    trimmed = [_order_brief(o) for o in rows[:limit]]
    return {
        'count': len(trimmed),
        'totalMatched': len(rows),
        'filter': {'status': status, 'customerId': cid},
        'orders': trimmed,
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def get_order(order_id: str) -> dict:
    """Get one order in full: texts, instructions, reward, assignees, proof URLs, metrics.

    Use it to check what a write tool actually created, or to read a task before
    reassigning it. order_id is the UUID from list_orders or from create_order.
    """
    oid = _uuid_str(order_id, 'order_id')
    return _order_full(await get_client().get_order(oid))


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def order_candidates(order_id: str) -> dict:
    """List executors who can already see this order in the pool (access-wise).

    This is the exact answer to "who can pick this up": it accounts for the whole
    access cascade — per-project access, per-client grants, "open to everyone" flags
    and executors who see all tasks. An empty list means nobody can claim the order;
    assign it explicitly with assign_order() or widen the access in the admin panel.

    Does NOT change anything and does NOT tell who is going to take it.
    """
    oid = _uuid_str(order_id, 'order_id')
    data = await get_client().get_order_candidates(oid)
    people = data.get('executors') or []
    return {
        'orderId': oid,
        'count': len(people),
        'executors': [
            {'id': e.get('id'), 'name': e.get('displayName'), 'username': e.get('username')}
            for e in people
        ],
        'note': (
            'Nobody is in scope — this order cannot be claimed by anyone right now.'
            if not people else ''
        ),
    }


# --- Запись -----------------------------------------------------------------------


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
))
async def create_order(
    order_type: OrderType,
    customer_id: str,
    reward_cents: int,
    project_id: str | None = None,
    subreddit: str | None = None,
    target_url: str | None = None,
    title: str | None = None,
    body: str = '',
    body_mode: BodyMode | None = None,
    instructions: str = '',
    quantity: int = 1,
    assigned_executor_id: str | None = None,
    as_draft: bool = False,
    confirm: bool = False,
) -> dict:
    """Create ONE real, paid order for executors, on behalf of the admin account.

    SPENDS REAL MONEY. Unless as_draft=True, the order goes live immediately: executors
    see it and can claim it, and an approved proof pays reward_cents out of the budget.
    There is no "undo" tool here — cancelling a live order is done in the admin panel.

    Two-step by design: the first call with confirm=False creates NOTHING and returns a
    preview with the exact payload and the cost in dollars. Show that to the human, get
    an explicit yes, then repeat the identical call with confirm=True.

    MONEY IS IN CENTS. reward_cents=500 is $5.00, reward_cents=50000 is $500.00.
    It is the total budget of the order, not a per-action rate: for mass_* orders with
    quantity=N the executor earns reward_cents * (approved actions / N).

    Required fields depend on order_type:
      post                       -> subreddit + title (+ body, posts are always ready text)
      comment, reply             -> target_url (link to the Reddit post/comment to answer)
      mass_comment, mass_post    -> subreddit + quantity (1..20)

    body vs instructions: body is the content itself, instructions is guidance for the
    executor. body_mode="text" means body is the final text to publish as is (mandatory
    for post); body_mode="brief" means body is a brief and the executor writes the text.
    Comments/replies default to "brief", mass_* to "brief".

    Hyperlinks go inside body as markdown: [anchor text](https://example.com). That is
    what Reddit renders as a link, and it survives the executor copying the text. Do not
    put HTML in body — it would be published literally. When body holds a link, the
    executor is told not to drop it; you do not need to repeat that in instructions.

    assigned_executor_id assigns the order personally (use list_executors); leave it
    empty to drop the order into the pool of everyone who has access — check with
    order_candidates() afterwards that the pool is not empty.

    as_draft=True parks the order as a draft that nobody can see or claim; publish it
    later with open_order(). Use it when the text still needs review.

    Does NOT: create clients or projects, set user statuses, pay anyone out, or edit an
    existing order.
    """
    cid = _uuid_str(customer_id, 'customer_id')
    pid = _uuid_str(project_id, 'project_id') if project_id else None
    eid = _uuid_str(assigned_executor_id, 'assigned_executor_id') if assigned_executor_id else None

    _check_reward(reward_cents)
    _validate_shape(order_type, subreddit, target_url, title, body, body_mode)

    is_mass = order_type in ('mass_comment', 'mass_post')
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise ToolError('quantity must be an integer >= 1.')
    if is_mass and quantity > MAX_QUANTITY:
        raise ToolError(f'quantity must be between 1 and {MAX_QUANTITY} for mass orders.')
    if not is_mass and quantity != 1:
        raise ToolError(
            f'quantity is only meaningful for mass_comment / mass_post; {order_type} orders are '
            'always a single action. Create several orders instead.'
        )

    payload: dict[str, Any] = {
        'type': order_type,
        'customerId': cid,
        'projectId': pid,
        'subreddit': subreddit,
        'targetUrl': target_url,
        'title': title,
        'body': body,
        'bodyMode': body_mode,
        'instructions': instructions,
        'rewardCents': reward_cents,
        'quantity': quantity if is_mass else 1,
        'assignedExecutorId': eid,
        'asDraft': bool(as_draft),
    }

    warnings = _reward_warnings(reward_cents, per_what='for this order')
    if not as_draft and not eid:
        warnings.append(
            'No executor assigned and as_draft=False: the order goes straight to the shared pool '
            'and can be claimed immediately.'
        )
    if not pid:
        warnings.append('No project_id: the executor gets no product brief beyond instructions.')

    if not confirm:
        # Осознанный стоп: подтверждение параметров человеком до траты денег.
        return {
            'status': 'CONFIRMATION_REQUIRED',
            'created': False,
            'message': (
                'Nothing was created. Show the human the payload and the cost below, get an '
                'explicit approval, then call create_order again with the same arguments plus '
                'confirm=true.'
            ),
            'preview': payload,
            'cost': {
                'rewardCents': reward_cents,
                'reward': usd(reward_cents),
                'maxPayout': usd(reward_cents),
                'note': 'reward_cents is the total budget of this order, in cents.',
            },
            'warnings': warnings,
        }

    order = await get_client().create_order(payload)
    return {
        'status': 'CREATED',
        'created': True,
        'order': _order_full(order),
        'warnings': warnings,
        'note': (
            'Draft is invisible to executors — call open_order() to publish it.'
            if as_draft else
            'The order is live. Use order_candidates() to check somebody can actually see it.'
        ),
    }


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
))
async def create_orders_batch(
    order_type: Literal['mass_comment', 'mass_post'],
    customer_id: str,
    subreddit: str,
    reward_cents: int,
    executor_ids: list[str] | None = None,
    assign_all: bool = False,
    project_id: str | None = None,
    title: str | None = None,
    body: str = '',
    body_mode: BodyMode | None = 'brief',
    instructions: str = '',
    quantity: int = 3,
    confirm: bool = False,
) -> dict:
    """Create ONE mass order PER EXECUTOR from a single brief. SPENDS REAL MONEY.

    MONEY IS IN CENTS: reward_cents=500 is $5.00 per executor.

    Multiplies cost: N executors x reward_cents each. With 6 executors and
    reward_cents=500 the total budget is $30.00, not $5.00. The preview computes it
    for you — read it before confirming.

    Only mass_comment / mass_post. Every created order is assigned personally to its
    executor and goes live immediately (there is no draft mode in the batch endpoint).

    executor_ids: UUIDs from list_executors. assign_all=True instead targets every
    executor who has access to this client (grants + per-project access) — the exact
    set is resolved server-side, so prefer explicit executor_ids when the money matters.

    quantity is how many comments/posts EACH executor must publish (1..20), and
    reward_cents is that executor's total for all of them.

    Same two-step confirmation as create_order: confirm=False returns a preview and
    creates nothing.

    Does NOT support post/comment/reply — use create_order for those, one per call.
    """
    cid = _uuid_str(customer_id, 'customer_id')
    pid = _uuid_str(project_id, 'project_id') if project_id else None
    ids = [_uuid_str(x, 'executor_ids[]') for x in (executor_ids or [])]

    if not ids and not assign_all:
        raise ToolError(
            'Pass executor_ids (from list_executors) or assign_all=True. A batch with no '
            'executors creates nothing.'
        )
    if not (subreddit or '').strip():
        raise ToolError('subreddit is required for mass orders.')
    _check_reward(reward_cents)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_QUANTITY:
        raise ToolError(f'quantity must be an integer between 1 and {MAX_QUANTITY}.')
    _validate_shape(order_type, subreddit, None, title, body, body_mode)

    payload: dict[str, Any] = {
        'type': order_type,
        'customerId': cid,
        'projectId': pid,
        'subreddit': subreddit,
        'title': title,
        'body': body,
        'bodyMode': body_mode,
        'instructions': instructions,
        'rewardCents': reward_cents,
        'quantity': quantity,
        'executorIds': ids,
        'assignAll': bool(assign_all),
    }

    warnings = _reward_warnings(reward_cents, per_what='per executor')
    if assign_all:
        warnings.append(
            'assign_all=True: the executor set (and therefore the total cost) is resolved on the '
            'server and may be larger than expected. Total below is unknown until creation.'
        )

    if not confirm:
        known = len(ids) if ids and not assign_all else None
        return {
            'status': 'CONFIRMATION_REQUIRED',
            'created': False,
            'message': (
                'Nothing was created. Show the human the payload and the total cost below, get '
                'an explicit approval, then repeat the call with confirm=true.'
            ),
            'preview': payload,
            'cost': {
                'ordersToCreate': known,
                'rewardCentsPerExecutor': reward_cents,
                'rewardPerExecutor': usd(reward_cents),
                'maxTotal': usd(reward_cents * known) if known is not None else 'unknown',
                'note': (
                    f'{known} executors x {usd(reward_cents)} each'
                    if known is not None else
                    'assign_all=True — the number of executors is decided by the server.'
                ),
            },
            'warnings': warnings,
        }

    result = await get_client().create_orders_batch(payload)
    created = int(result.get('created') or 0)
    return {
        'status': 'CREATED',
        'created': True,
        'ordersCreated': created,
        'orderIds': result.get('orderIds') or [],
        'totalBudget': usd(reward_cents * created),
        'warnings': warnings,
        'note': 'All of them are live and personally assigned. Verify with list_orders.',
    }


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def assign_order(
    order_id: str,
    executor_ids: list[str],
    open_to_everyone: bool = False,
) -> dict:
    """Assign an existing order to specific executors (replaces the current assignment).

    The list is the whole new assignment, not an addition: passing one id after two
    unassigns the other one. Passing an empty list clears personal assignment and puts
    the order back into the access-based pool.

    open_to_everyone=True (only with an empty executor_ids) exposes the order to EVERY
    active executor on the platform, ignoring per-client access grants. Use sparingly.

    Works only while the order is draft / pending / open — an order already claimed or
    submitted cannot be reassigned here. Does not change the reward.
    """
    oid = _uuid_str(order_id, 'order_id')
    ids = [_uuid_str(x, 'executor_ids[]') for x in (executor_ids or [])]
    if open_to_everyone and ids:
        raise ToolError(
            'open_to_everyone=True works only with an empty executor_ids: it is the opposite of '
            'a personal assignment.'
        )
    order = await get_client().assign_order(
        oid, {'executorIds': ids, 'openToEveryone': bool(open_to_everyone)},
    )
    return {'status': 'ASSIGNED', 'order': _order_brief(order)}


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True,
))
async def open_order(order_id: str) -> dict:
    """Publish an admin draft: draft -> open. The order becomes claimable immediately.

    Use it after create_order(as_draft=True) once the text has been reviewed. From this
    moment the order can be picked up and paid for.

    Only works on orders in status "draft"; anything else returns a 409. Cancelling a
    published order is not available through this MCP server — use the admin panel.
    """
    oid = _uuid_str(order_id, 'order_id')
    order = await get_client().open_order(oid)
    return {'status': 'OPENED', 'order': _order_brief(order)}


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
))
async def cancel_order(order_id: str, confirm: bool = False) -> dict:
    """Cancel an order: it stops being visible and claimable. THIS CAN TAKE WORK AWAY.

    This is the undo for create_order, but it is not a "delete the draft" button. If the
    order is in status "claimed", an executor is already working on it right now:
    cancelling marks their assignment as abandoned and they lose the work they started.
    Read the status in the preview before confirming, and if it is claimed, tell the
    human that a live person is affected.

    Cancellable statuses (admin): draft, pending, open, needs_revision, claimed.
    NOT cancellable: submitted (a proof is waiting for review — review or reject it
    instead), retention, completed, and an order already cancelled. The backend rejects
    those with a 409 that names the current status.

    Money already credited to an executor's ledger is NOT clawed back by cancelling.

    Two-step like create_order: with confirm=False nothing is cancelled — the tool only
    reads the order and returns what would be cancelled (type, status, reward, client,
    who it is assigned to). Show that to the human, then repeat with confirm=True.
    """
    oid = _uuid_str(order_id, 'order_id')
    client = get_client()

    if not confirm:
        # Только чтение: показать, что именно исчезнет, до того как оно исчезнет.
        order = _order_brief(await client.get_order(oid))
        status = order.get('status')
        warnings: list[str] = []
        if status == 'claimed':
            warnings.append(
                'Status is "claimed": an executor is working on this order right now. '
                'Cancelling abandons their assignment and they lose the started work.'
            )
        if status in ('submitted', 'retention', 'completed', 'cancelled'):
            warnings.append(
                f'Status is "{status}" — the backend will refuse to cancel it (409). '
                'Nothing here can force it.'
            )
        if order.get('assignedExecutorNames'):
            warnings.append(
                'Personally assigned to: ' + ', '.join(order['assignedExecutorNames'])
            )
        return {
            'status': 'CONFIRMATION_REQUIRED',
            'cancelled': False,
            'message': (
                'Nothing was cancelled. Show the human what is about to be cancelled, get an '
                'explicit approval, then call cancel_order again with confirm=true.'
            ),
            'preview': order,
            'warnings': warnings,
        }

    return {
        'status': 'CANCELLED',
        'cancelled': True,
        'order': _order_brief(await client.cancel_order(oid)),
        'note': (
            'Any active assignment on this order is now abandoned. Money already credited to '
            'the ledger is not returned by cancelling.'
        ),
    }


def main() -> None:
    """Точка входа консольного скрипта feedheat-mcp. Транспорт — stdio."""
    server.run(transport='stdio')


if __name__ == '__main__':
    main()


__all__ = [
    'ApiError',
    'get_client',
    'main',
    'server',
    'set_client',
]
