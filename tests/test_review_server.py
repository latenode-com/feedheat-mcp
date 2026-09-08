# Сервер разбора сдач: работа прибита к сессии, применить вердикт нечем.
from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from feedheat_mcp import review_server as rev
from tests.conftest import api_error, json_response, make_client

WORK = '66666666-6666-6666-6666-666666666666'


def use_client(handler):
    client, rec = make_client(handler)
    rev.set_client(client)
    return rec


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('FEEDHEAT_ASSIGNMENT_ID', WORK)
    return WORK


async def test_without_an_assignment_every_tool_refuses():
    use_client(json_response({}))
    with pytest.raises(ToolError) as e:
        await rev.read_submission()
    assert 'FEEDHEAT_ASSIGNMENT_ID' in str(e.value)


async def test_assignment_id_is_not_a_tool_parameter():
    """Ни один инструмент не принимает чужую работу — назвать её нечем."""
    import inspect

    for tool in (rev.read_submission, rev.check_publication, rev.propose_verdict,
                 rev.escalate):
        params = set(inspect.signature(tool).parameters)
        assert not (params & {'assignment_id', 'assignmentId', 'assignment'}), tool


async def test_reads_go_to_the_pinned_assignment(pinned):
    rec = use_client(json_response({'assignmentId': pinned}))
    await rev.read_submission()
    assert rec.last.url.path == f'/api/support/agent/submissions/{pinned}'
    await rev.check_publication()
    assert rec.last.url.path.endswith(f'{pinned}/check')


async def test_verdict_is_a_draft_and_says_nobody_was_paid(pinned):
    rec = use_client(json_response({'draftId': 'd1', 'verdict': 'approve',
                                    'status': 'pending', 'applied': False}))
    out = await rev.propose_verdict(verdict='approve', reason='',
                                    reasoning='проверка нашла комментарий')
    assert out['applied'] is False
    assert 'nobody paid' in out['note']
    assert rec.json_body()['verdict'] == 'approve'


async def test_rejection_without_a_reason_never_reaches_the_network(pinned):
    rec = use_client(json_response({}))
    with pytest.raises(ToolError):
        await rev.propose_verdict(verdict='reject', reason='   ', reasoning='r')
    assert rec.calls == 0


async def test_approval_may_have_no_reason(pinned):
    """Принятой работе объяснение не нужно — человеку и так хорошо."""
    rec = use_client(json_response({'draftId': 'd2', 'applied': False}))
    await rev.propose_verdict(verdict='approve', reason='', reasoning='всё на месте')
    assert rec.calls == 1


async def test_verdict_without_reasoning_is_refused(pinned):
    rec = use_client(json_response({}))
    with pytest.raises(ToolError):
        await rev.propose_verdict(verdict='approve', reason='', reasoning='')
    assert rec.calls == 0


async def test_escalation_goes_to_the_human_queue(pinned):
    rec = use_client(json_response({'itemId': 'i1', 'kind': 'unclear',
                                    'status': 'open'}))
    out = await rev.escalate(kind='unclear', reason='проверка вернула unknown')
    assert rec.last.url.path.endswith(f'/assignments/{pinned}/attention')
    assert 'Nothing applied' in out['note']


async def test_missing_scope_is_not_retried(pinned):
    rec = use_client(api_error(403, 'SCOPE_DENIED', 'API key lacks required scope'))
    with pytest.raises(ToolError):
        await rev.propose_verdict(verdict='approve', reason='', reasoning='r')
    assert rec.calls == 1


async def test_api_key_never_appears_in_errors(pinned):
    import httpx

    from tests.conftest import TEST_KEY

    use_client(lambda _r: httpx.Response(500, text=f'boom {TEST_KEY}'))
    with pytest.raises(ToolError) as e:
        await rev.read_submission()
    assert TEST_KEY not in str(e.value)
