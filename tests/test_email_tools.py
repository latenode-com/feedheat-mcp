# Схемы и confirm-ворота email-кампаний на админском MCP.
from __future__ import annotations

import inspect

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from feedheat_mcp import email_tools as et
from feedheat_mcp import server as srv
from tests.conftest import json_response, make_client

CAMPAIGN = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


def use_client(handler):
    client, rec = make_client(handler)
    srv.set_client(client)
    return rec


def campaign_payload(**over):
    data = {
        'id': CAMPAIGN,
        'name': 'Come back',
        'subject': 'Hi',
        'htmlBody': '<p>Hello</p>',
        'textBody': '',
        'cohort': {'role': 'executor'},
        'trigger': 'manual',
        'scheduleAt': None,
        'cron': '',
        'timezone': 'UTC',
        'status': 'draft',
        'sentCount': 0,
        'failedCount': 0,
        'skippedCount': 0,
    }
    data.update(over)
    return data


def test_email_tool_names_are_registered():
    names = {
        'list_email_campaigns', 'get_email_campaign', 'create_email_campaign',
        'update_email_campaign', 'preview_email_campaign_audience',
        'preview_email_campaign_html', 'schedule_email_campaign',
        'send_email_campaign', 'pause_email_campaign', 'cancel_email_campaign',
    }
    for name in names:
        assert hasattr(et, name), name
        assert inspect.iscoroutinefunction(getattr(et, name))


async def test_create_requires_name():
    use_client(json_response(campaign_payload(), 201))
    with pytest.raises(ToolError):
        await et.create_email_campaign(name='  ')


async def test_create_posts_camel_case_payload():
    rec = use_client(json_response(campaign_payload(), 201))
    result = await et.create_email_campaign(
        name='Come back',
        subject='Hi {{name}}',
        html_body='<p>x</p>',
        cohort={'never_claimed': True},
    )
    assert result['status'] == 'CREATED'
    body = rec.json_body()
    assert body['htmlBody'] == '<p>x</p>'
    assert body['cohort'] == {'never_claimed': True}
    assert rec.last.url.path.endswith('/api/admin/email-campaigns')


async def test_send_confirm_false_still_hits_api_for_preview():
    """Confirm живёт на бэкенде: MCP всегда зовёт ручку, а она ничего не шлёт."""
    rec = use_client(json_response({
        'status': 'CONFIRMATION_REQUIRED',
        'sent': False,
        'audienceCount': 12,
        'campaign': campaign_payload(),
    }))
    result = await et.send_email_campaign(campaign_id=CAMPAIGN, confirm=False)
    assert result['status'] == 'CONFIRMATION_REQUIRED'
    assert result['sent'] is False
    assert rec.calls == 1
    assert rec.json_body()['confirm'] is False


async def test_send_confirm_true_forwards_flag():
    rec = use_client(json_response({
        'dryRun': False, 'sent': 3, 'failed': 0, 'skipped': 0,
        'campaign': campaign_payload(status='sent', sentCount=3),
    }))
    result = await et.send_email_campaign(campaign_id=CAMPAIGN, confirm=True, limit=10)
    assert result['sent'] == 3
    assert rec.json_body() == {'confirm': True, 'dryRun': False, 'limit': 10}


async def test_bad_campaign_id_rejected_before_network():
    rec = use_client(json_response(campaign_payload()))
    with pytest.raises(ToolError) as exc:
        await et.get_email_campaign('not-a-uuid')
    assert 'UUID' in str(exc.value)
    assert rec.calls == 0


async def test_update_requires_at_least_one_field():
    use_client(json_response(campaign_payload()))
    with pytest.raises(ToolError):
        await et.update_email_campaign(campaign_id=CAMPAIGN)


async def test_preview_audience_without_campaign_posts_cohort():
    rec = use_client(json_response({'count': 2, 'sample': [], 'cohortKeys': []}))
    out = await et.preview_email_campaign_audience(cohort={'role': 'executor'}, sample=5)
    assert out['count'] == 2
    assert rec.json_body() == {'cohort': {'role': 'executor'}, 'sample': 5}
