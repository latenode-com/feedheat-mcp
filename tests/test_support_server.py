# Сервер поддержки: разговор прибит к сессии, отправки нет, отказ отвечать — отдельный путь.
from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from feedheat_mcp import support_server as sup
from tests.conftest import api_error, json_response, make_client

THREAD = '55555555-5555-5555-5555-555555555555'


def use_client(handler):
    client, rec = make_client(handler)
    sup.set_client(client)
    return rec


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('FEEDHEAT_THREAD_ID', THREAD)
    return THREAD


# --- Разговор прибит к сессии ----------------------------------------------

async def test_without_thread_id_every_tool_refuses():
    """Пустой FEEDHEAT_THREAD_ID — это не «читай любой», а «не работай»."""
    use_client(json_response({}))
    with pytest.raises(ToolError) as e:
        await sup.read_conversation()
    assert 'FEEDHEAT_THREAD_ID' in str(e.value)


async def test_thread_id_is_not_a_tool_parameter():
    """Ни один инструмент не принимает идентификатор разговора.

    Это и есть защита от письма «ответь вот по тому обращению»: назвать чужой
    тред нечем.
    """
    import inspect

    for tool in (sup.read_conversation, sup.who_wrote_us, sup.their_work,
                 sup.propose_reply, sup.escalate):
        params = set(inspect.signature(tool).parameters)
        assert not (params & {'thread_id', 'threadId', 'thread'}), tool


async def test_reads_go_to_the_pinned_thread(pinned):
    rec = use_client(json_response({'threadId': pinned, 'messages': []}))
    await sup.read_conversation()
    assert rec.last.url.path == f'/api/support/agent/threads/{pinned}'
    await sup.who_wrote_us()
    assert rec.last.url.path.endswith(f'{pinned}/person')
    await sup.their_work()
    assert rec.last.url.path.endswith(f'{pinned}/orders')


# --- Черновик, а не отправка -----------------------------------------------

async def test_propose_reply_saves_a_draft_and_says_it_was_not_sent(pinned):
    rec = use_client(json_response({'draftId': 'd1', 'status': 'pending',
                                    'verdict': 'reply'}))
    out = await sup.propose_reply(body='Here is why.', reasoning='review_note',
                                  facts={'availableCents': 300})
    assert out['sent'] is False
    assert rec.last.method == 'POST'
    assert rec.last.url.path.endswith('/draft')
    body = rec.json_body()
    assert body['verdict'] == 'reply'
    assert body['facts'] == {'availableCents': 300}


async def test_empty_body_is_refused_before_the_network(pinned):
    rec = use_client(json_response({}))
    with pytest.raises(ToolError):
        await sup.propose_reply(body='   ', reasoning='что-то')
    assert rec.calls == 0


async def test_reply_without_reasoning_is_refused(pinned):
    """Черновик, который нечем проверить, отправлять нельзя — значит и писать незачем."""
    rec = use_client(json_response({}))
    with pytest.raises(ToolError):
        await sup.propose_reply(body='Hello', reasoning='')
    assert rec.calls == 0


async def test_escalate_is_a_separate_path(pinned):
    rec = use_client(json_response({'draftId': 'd2', 'status': 'pending',
                                    'verdict': 'escalate'}))
    out = await sup.escalate(reason='просит вернуть деньги, нужен человек')
    assert out['sent'] is False
    assert rec.json_body()['verdict'] == 'escalate'
    assert rec.json_body()['body'] == ''


async def test_escalate_needs_a_reason(pinned):
    rec = use_client(json_response({}))
    with pytest.raises(ToolError):
        await sup.escalate(reason='  ')
    assert rec.calls == 0


async def test_run_label_travels_with_the_draft(pinned, monkeypatch):
    monkeypatch.setenv('FEEDHEAT_RUN_LABEL', 'cron-2026-09-08')
    rec = use_client(json_response({'draftId': 'd3', 'status': 'pending'}))
    await sup.propose_reply(body='x', reasoning='y')
    assert rec.json_body()['runLabel'] == 'cron-2026-09-08'


# --- Ошибки бэкенда доходят понятными --------------------------------------

async def test_unknown_person_reads_as_a_clear_refusal(pinned):
    use_client(api_error(404, 'PERSON_UNKNOWN',
                         'This thread is not linked to a verified account'))
    with pytest.raises(ToolError) as e:
        await sup.who_wrote_us()
    assert 'PERSON_UNKNOWN' in str(e.value)


async def test_missing_scope_is_not_retried_blindly(pinned):
    rec = use_client(api_error(403, 'SCOPE_DENIED', 'API key lacks required scope'))
    with pytest.raises(ToolError):
        await sup.propose_reply(body='x', reasoning='y')
    assert rec.calls == 1


async def test_api_key_never_appears_in_tool_errors(pinned):
    from tests.conftest import TEST_KEY

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f'boom {TEST_KEY}')

    use_client(handler)
    with pytest.raises(ToolError) as e:
        await sup.read_conversation()
    assert TEST_KEY not in str(e.value)
