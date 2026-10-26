# MCP-инструменты email-кампаний на админском сервере.
#
# Регистрируются на том же MCPServer, что и заказы (server.py импортирует этот
# модуль). Отправка — двухшаговая с confirm, как create_order: письмо тысячам
# людей отозвать нельзя.
from __future__ import annotations

import uuid
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from feedheat_mcp.server import get_client, server

CampaignStatus = Literal['draft', 'scheduled', 'sending', 'sent', 'paused', 'cancelled']
Trigger = Literal[
    'manual',
    'schedule',
    'new_task_available',
    'executor_idle',
    'order_status',
    'assignment_claimed',
    'assignment_released',
    'user_registered',
    'email_verified',
    'user_approved',
]

COHORT_DOC = (
    'Cohort is a JSON filter over users. Supported keys: role (default executor), '
    'status (default active), email_verified, notify_new_tasks (default true for '
    'executors), created_after/before, last_login_after/before, last_claim_after/'
    'before, never_claimed, has_assignments, project_id, user_ids, exclude_user_ids, '
    'emails, limit. Unknown keys are rejected.'
)

TRIGGER_DOC = (
    'Triggers: '
    'manual (send tool only); '
    'schedule (schedule_at and/or 5-field cron; runner every minute); '
    'new_task_available (order becomes open; optional trigger_config: '
    'min_reward_cents, project_ids, order_types); '
    'executor_idle (scanner; required trigger_config.days_idle, optional '
    'never_claimed_counts); '
    'order_status (required trigger_config.to statuses + audience '
    'customer|executor|both); '
    'assignment_claimed; '
    'assignment_released (optional reasons: released|expired|cancelled); '
    'user_registered; email_verified; user_approved. '
    'Event triggers stay armed (status=scheduled) after partial sends; '
    'Delivery (campaign,user) is once-per-user. Arm with '
    'schedule_email_campaign(confirm=true) — no schedule_at for events.'
)


def _uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError):
        raise ToolError(
            f'{field} must be a UUID from list_email_campaigns / get_email_campaign. '
            f'Got: {value!r}'
        ) from None


@server.tool(annotations=ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def list_email_campaigns(
    status: CampaignStatus | None = None,
    limit: int = 50,
) -> dict:
    """List email campaigns (newest first).

    status filters by draft|scheduled|sending|sent|paused|cancelled. Needs emails:read.
    """
    rows = await get_client().list_email_campaigns(status=status, limit=limit)
    return {'count': len(rows), 'campaigns': rows}


@server.tool(annotations=ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def get_email_campaign(campaign_id: str) -> dict:
    """Full email campaign: subject, html, cohort, trigger, triggerConfig, schedule, counts."""
    return await get_client().get_email_campaign(_uuid(campaign_id, 'campaign_id'))


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True,
))
async def create_email_campaign(
    name: str,
    subject: str = '',
    html_body: str = '',
    text_body: str = '',
    cohort: dict[str, Any] | None = None,
    trigger: Trigger = 'manual',
    trigger_config: dict[str, Any] | None = None,
    schedule_at: str | None = None,
    cron: str = '',
    timezone: str = 'UTC',
) -> dict:
    f"""Create a draft email campaign. Does NOT send anything.

    HTML may use placeholders: {{{{name}}}}, {{{{email}}}}, {{{{username}}}}, {{{{unsubscribe_url}}}}.
    {COHORT_DOC}
    {TRIGGER_DOC}
    """
    if not (name or '').strip():
        raise ToolError('name is required')
    payload: dict[str, Any] = {
        'name': name.strip(),
        'subject': subject or '',
        'htmlBody': html_body or '',
        'textBody': text_body or '',
        'cohort': cohort or {},
        'trigger': trigger,
        'triggerConfig': trigger_config or {},
        'scheduleAt': schedule_at,
        'cron': cron or '',
        'timezone': timezone or 'UTC',
    }
    created = await get_client().create_email_campaign(payload)
    return {
        'status': 'CREATED',
        'campaign': created,
        'note': (
            'Draft only — nothing was emailed. Edit with update_email_campaign, check '
            'audience with preview_email_campaign_audience, then schedule_email_campaign '
            '(arms event triggers or sets cron/schedule_at) or send_email_campaign '
            '(both need confirm=true).'
        ),
    }


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def update_email_campaign(
    campaign_id: str,
    name: str | None = None,
    subject: str | None = None,
    html_body: str | None = None,
    text_body: str | None = None,
    cohort: dict[str, Any] | None = None,
    trigger: Trigger | None = None,
    trigger_config: dict[str, Any] | None = None,
    schedule_at: str | None = None,
    cron: str | None = None,
    timezone: str | None = None,
    status: Literal['draft', 'paused'] | None = None,
) -> dict:
    f"""Partial update of an email campaign (time, cohort, trigger, trigger_config, html, …).

    Cannot edit sending/cancelled campaigns. status may only be set to draft or paused
    here — use schedule/send/cancel tools for the other transitions.
    {TRIGGER_DOC}
    """
    cid = _uuid(campaign_id, 'campaign_id')
    payload: dict[str, Any] = {}
    if name is not None:
        payload['name'] = name
    if subject is not None:
        payload['subject'] = subject
    if html_body is not None:
        payload['htmlBody'] = html_body
    if text_body is not None:
        payload['textBody'] = text_body
    if cohort is not None:
        payload['cohort'] = cohort
    if trigger is not None:
        payload['trigger'] = trigger
    if trigger_config is not None:
        payload['triggerConfig'] = trigger_config
    if schedule_at is not None:
        payload['scheduleAt'] = schedule_at
    if cron is not None:
        payload['cron'] = cron
    if timezone is not None:
        payload['timezone'] = timezone
    if status is not None:
        payload['status'] = status
    if not payload:
        raise ToolError('Pass at least one field to update')
    return {
        'status': 'UPDATED',
        'campaign': await get_client().update_email_campaign(cid, payload),
    }


@server.tool(annotations=ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def preview_email_campaign_audience(
    campaign_id: str | None = None,
    cohort: dict[str, Any] | None = None,
    sample: int = 10,
) -> dict:
    f"""Count + sample emails for a campaign's cohort (or an ad-hoc cohort JSON).

    Pass campaign_id to use the saved filter, or cohort to dry-run a filter before create.
    {COHORT_DOC}
    """
    if campaign_id:
        cid = _uuid(campaign_id, 'campaign_id')
        return await get_client().preview_email_campaign_audience(cid, sample=sample)
    return await get_client().preview_email_campaign_audience(
        cohort=cohort or {}, sample=sample,
    )


@server.tool(annotations=ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def preview_email_campaign_html(
    subject: str = '',
    html_body: str = '',
    text_body: str = '',
    user_id: str | None = None,
) -> dict:
    """Dry-run render of subject/html with placeholders filled for a user (or a stub)."""
    payload: dict[str, Any] = {
        'subject': subject,
        'htmlBody': html_body,
        'textBody': text_body,
    }
    if user_id:
        payload['userId'] = _uuid(user_id, 'user_id')
    return await get_client().preview_email_campaign_html(payload)


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
))
async def schedule_email_campaign(
    campaign_id: str,
    schedule_at: str | None = None,
    cron: str = '',
    timezone: str | None = None,
    confirm: bool = False,
) -> dict:
    """Schedule (trigger=schedule) or arm (event triggers) a campaign.

    Two-step: confirm=false returns audience size and does nothing; confirm=true
    marks the campaign scheduled/armed.

    For trigger=schedule: pass schedule_at (ISO) and/or a 5-field cron
    (e.g. "0 9 * * 1" = Mondays 09:00 in timezone). Runner fires every minute.

    For event triggers (new_task_available, executor_idle, order_status,
    assignment_*, user_*, email_verified): omit schedule_at/cron — confirm arms
    the campaign so domain events / the idle scanner can deliver.
    """
    cid = _uuid(campaign_id, 'campaign_id')
    return await get_client().schedule_email_campaign(cid, {
        'scheduleAt': schedule_at,
        'cron': cron or '',
        'timezone': timezone,
        'confirm': bool(confirm),
    })


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
))
async def send_email_campaign(
    campaign_id: str,
    confirm: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Send a campaign now to its cohort.

    REAL EMAILS. Two-step: confirm=false returns audience preview and sends nothing.
    dry_run=true simulates without confirm. Already-delivered recipients are skipped.
    """
    cid = _uuid(campaign_id, 'campaign_id')
    return await get_client().send_email_campaign(cid, {
        'confirm': bool(confirm),
        'dryRun': bool(dry_run),
        'limit': limit,
    })


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
))
async def pause_email_campaign(campaign_id: str) -> dict:
    """Pause a draft/scheduled (armed) campaign so the runner/events will not fire it."""
    cid = _uuid(campaign_id, 'campaign_id')
    return {
        'status': 'PAUSED',
        'campaign': await get_client().pause_email_campaign(cid),
    }


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
))
async def cancel_email_campaign(campaign_id: str) -> dict:
    """Cancel a campaign permanently (cannot send afterwards)."""
    cid = _uuid(campaign_id, 'campaign_id')
    return {
        'status': 'CANCELLED',
        'campaign': await get_client().cancel_email_campaign(cid),
    }
