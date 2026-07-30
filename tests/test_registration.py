# Регистрация инструментов в MCP: имена, обязательные поля, аннотации.
# Ловит поломку схемы (напр. переименованный параметр) раньше, чем это увидит модель.
from __future__ import annotations

import pytest

from feedheat_mcp.server import server

EXPECTED = {
    'list_clients', 'list_projects', 'list_executors', 'list_orders', 'get_order',
    'order_candidates', 'create_order', 'create_orders_batch', 'assign_order', 'open_order',
    'cancel_order',
}


@pytest.fixture
async def tools():
    return {t.name: t for t in await server.list_tools()}


async def test_all_tools_are_registered(tools):
    assert set(tools) == EXPECTED


async def test_create_order_requires_money_explicitly(tools):
    schema = tools['create_order'].input_schema
    assert set(schema['required']) == {'order_type', 'customer_id', 'reward_cents'}
    # confirm по умолчанию false — иначе двухшаговое подтверждение развалится
    assert schema['properties']['confirm']['default'] is False


async def test_confirmation_gated_tools_default_to_false(tools):
    """Двухшаговое подтверждение — у всех, кто тратит деньги или отбирает работу."""
    for name in ('create_order', 'create_orders_batch', 'cancel_order'):
        assert tools[name].input_schema['properties']['confirm']['default'] is False


async def test_cancel_order_warns_the_model_about_live_executors(tools):
    text = tools['cancel_order'].description or ''
    assert 'claimed' in text
    assert 'abandoned' in text


async def test_write_tools_are_marked_as_such(tools):
    for name in ('create_order', 'create_orders_batch', 'cancel_order'):
        ann = tools[name].annotations
        assert ann.read_only_hint is False
        assert ann.destructive_hint is True
        assert ann.idempotent_hint is False
    for name in ('list_clients', 'list_orders', 'get_order', 'order_candidates'):
        assert tools[name].annotations.read_only_hint is True


async def test_money_units_are_spelled_out_for_the_model(tools):
    """Описание обязано объяснять центы — иначе награда уедет в 100 раз."""
    for name in ('create_order', 'create_orders_batch'):
        text = tools[name].description or ''
        assert 'CENTS' in text
        assert '$5.00' in text or 'x reward_cents' in text
        assert 'REAL MONEY' in text
