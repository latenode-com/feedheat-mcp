# MCP-сервер разбора сдач: одна работа, три чтения, один предложенный вердикт.
#
# Устройство то же, что у сервера поддержки, и по тем же причинам: работа
# прибита к сессии переменной окружения, инструменты не принимают её
# идентификатор, применить вердикт нечем. Но цена ошибки здесь другая, и это
# меняет одну вещь.
#
# ПРИЁМКА ТРАТИТ ДЕНЬГИ. Одобренная работа создаёт начисление исполнителю
# (services._credit_assignment). Поэтому агент не одобряет и не отклоняет: он
# кладёт вердикт черновиком, а нажимает человек. Бэкенд закрывает применение
# для API-ключей навсегда (apps/support/api.py, deny_api_key) — это не
# договорённость, а замок.
#
# Второе следствие: отказ дороже приёмки. Приняли лишнее — потеряли доллар.
# Отказали зря — человек потерял оплаченный день и ушёл. При равных сомнениях
# правильный ход не «отклонить», а «отдать человеку».
from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from feedheat_mcp.client import AdminClient, ApiError, ConfigError

server = MCPServer(
    name='feedheat-review',
    version='0.1.0',
    instructions=(
        'You are reviewing ONE submitted task for FeedHeat Crowd, a marketplace where '
        'people are paid to post and comment on Reddit. Every tool works on that one '
        'submission; there is no way to reach another, and no way to approve anything.\n\n'
        'Work in this order: read_submission, then check_publication when the answer '
        'depends on whether the content is actually live, then propose_verdict — or '
        'escalate.\n\n'
        'The question you are answering is narrow: does what they published match what '
        'was asked, and is it still there. You are not judging the person.\n\n'
        'Rejecting costs more than approving. An approval we should not have given costs '
        'a couple of dollars. A rejection we should not have given costs somebody a day '
        'of paid work and usually loses them for good. When the two look equally likely, '
        'escalate instead of rejecting.\n\n'
        'Never invent a reason. Every claim in your verdict must come from a tool result. '
        'All money is in CENTS: rewardCents=200 means $2.00.'
    ),
)

_client: AdminClient | None = None


def set_client(client: AdminClient | None) -> None:
    global _client
    _client = client


def get_client() -> AdminClient:
    global _client
    if _client is None:
        try:
            _client = AdminClient()
        except ConfigError as exc:
            raise ToolError(str(exc)) from None
    return _client


def assignment_id() -> str:
    value = (os.getenv('FEEDHEAT_ASSIGNMENT_ID') or '').strip()
    if not value:
        raise ToolError(
            'FEEDHEAT_ASSIGNMENT_ID is not set. This server reviews exactly one '
            'submission and the id comes from the environment, not from the model. '
            'Whoever started this session must set it. Do not retry.'
        )
    return value


def _base() -> str:
    return f'/api/support/agent/submissions/{assignment_id()}'


async def _call(method: str, path: str = '', *, json: dict | None = None,
                scope: str = 'review:read') -> Any:
    try:
        return await get_client().request(method, _base() + path, json=json, scope=scope)
    except ApiError as exc:
        raise ToolError(str(exc)) from None


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def read_submission() -> dict:
    """Read the task and what was handed in for it.

    Start here. `task` is what the client asked for: the target, the brief or the exact
    text to post, and any instructions. `submission` is what came back: the proof links,
    the Reddit account it was posted from, and whether a screenshot is attached.

    bodyMode tells you how to read `body`. "text" means the exact text was given and
    should have been posted close to it. "brief" means the person wrote it themselves
    from the description, so judge the substance, not the wording.

    `person` is short on purpose: how long they have been here, their accept rate, and
    the moderator's own notes on their recent work. It is context for a borderline call,
    not a reason on its own — a good record does not make a missing comment present, and
    a poor one does not make a real comment fake.
    """
    return await _call('GET')


@server.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
async def check_publication() -> dict:
    """Go and look at Reddit: is the submitted content actually there?

    Call it whenever the verdict depends on the content being live, which is most of the
    time. Do not call it twice for the same submission — it drives a real browser through
    one of only two shared accounts, and those are also what the rest of the platform
    uses for metrics and trend scraping.

    verdict is "found", "gone" or "unknown".

    "unknown" is not a soft "gone". It means we could not see the page — Reddit rate
    limited us, the browser failed, the link is unreadable. Rejecting on "unknown" means
    punishing somebody for our own blindness: escalate instead.

    shareLinkUsed=true means they sent a /s/ short link, which opens the post rather than
    their comment. That is a real problem — nobody can verify the work — but it is a
    fixable mistake on their side, not a reason to reject outright. Say so in the reason
    and ask for the direct /comment/ link.
    """
    return await _call('POST', '/check')


@server.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
async def propose_verdict(verdict: str, reason: str, reasoning: str,
                          facts: dict | None = None) -> dict:
    """Propose what should happen to this submission. Nothing is applied and nobody is
    paid: a moderator reads this and decides.

    verdict: "approve", "reject", or "revision" (ask them to fix something without
    rejecting — the right choice when the work is real but the proof link is unusable).

    reason: goes to the person as the explanation. Write it for them, not for us. Say
    what is wrong and what to do about it, in one or two plain sentences. Never write
    "does not meet requirements" — that tells them nothing and they will write to
    support, which costs us more than the task did.

    reasoning: for the moderator, never sent. Say which tool result decided it, and what
    you were unsure about.

    facts: what you relied on, e.g. {"publicationCheck": "found", "redditAccount": "..."}.
    """
    text = (reason or '').strip()
    if verdict != 'approve' and not text:
        raise ToolError(
            'A rejection without a reason is not reviewable. Say what is wrong.')
    if not (reasoning or '').strip():
        raise ToolError('reasoning is required: a verdict nobody can check is not usable.')
    result = await _call('POST', '/verdict', scope='review:draft', json={
        'verdict': verdict, 'reason': text, 'reasoning': reasoning,
        'facts': facts or {},
        'runLabel': (os.getenv('FEEDHEAT_RUN_LABEL') or '').strip()[:120],
    })
    return {**result, 'note': 'Saved for a moderator. Nothing applied, nobody paid.'}


@server.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
async def escalate(kind: str, reason: str, facts: dict | None = None) -> dict:
    """Hand this submission to a person instead of deciding it.

    Use it when: the publication check came back "unknown"; the task itself is unclear or
    contradicts the target; the person is disputing an earlier decision; the work looks
    deliberately faked and that accusation needs a human; or you simply cannot tell.

    kind: "decision" for anything needing judgement, "unclear" when the data does not
    answer the question, "money" if it turns on a payment.

    Escalating a hard case is the cheapest good outcome we have. A wrong rejection costs
    a person their day and usually the relationship; waiting an hour costs neither.

    You can also propose_verdict first and escalate after, when part of it is clear and
    part is not.
    """
    text = (reason or '').strip()
    if not text:
        raise ToolError('reason is required: a person has to know what they are picking up.')
    try:
        result = await get_client().request(
            'POST', f'/api/support/agent/assignments/{assignment_id()}/attention',
            json={'kind': kind, 'reason': text, 'facts': facts or {},
                  'runLabel': (os.getenv('FEEDHEAT_RUN_LABEL') or '').strip()[:120]},
            scope='review:draft')
    except ApiError as exc:
        raise ToolError(str(exc)) from None
    return {**result, 'note': 'On the human queue. Nothing applied.'}


def main() -> None:
    """Точка входа feedheat-review-mcp. Транспорт — stdio."""
    server.run(transport='stdio')


if __name__ == '__main__':
    main()


__all__ = ['ApiError', 'assignment_id', 'escalate', 'get_client', 'main', 'server',
           'set_client']
