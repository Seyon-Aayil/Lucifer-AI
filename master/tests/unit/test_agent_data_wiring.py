"""Track A: specialist agents read real graph data instead of Phase 3 placeholders."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from master.agents.financial.agent import FinancialAgent
from master.agents.health.agent import HealthAgent
from master.agents.personal.agent import PersonalAgent


def _make(agent_cls, mcp=None):
    return agent_cls(
        agent_id=agent_cls.AGENT_ID,
        librarian=MagicMock(),
        llm_registry=MagicMock(),
        telemetry_emitter=MagicMock(),
        mcp_client=mcp,
    )


def _patch_graph(nodes_by_type: dict[str, list[dict]]):
    """Patch GraphClient.from_settings to serve canned nodes per node_type."""
    gc = MagicMock()
    gc.close = AsyncMock()

    async def _list(node_type, limit=10, order_by="decayScore"):
        return nodes_by_type.get(node_type, [])

    gc.list_nodes_by_type = AsyncMock(side_effect=_list)
    return patch(
        "master.agents.librarian.graph_client.GraphClient.from_settings",
        return_value=gc,
    )


@pytest.mark.asyncio
async def test_health_activity_summary_aggregates_without_raw_values():
    agent = _make(HealthAgent)
    records = [{"category": "activity", "value": 8000}, {"category": "activity", "value": 10000}]
    with _patch_graph({"HealthRecord": records}):
        text, _ = await agent._activity_summary(MagicMock())

    assert "pending Phase 3" not in text
    assert "2 record" in text
    # Aggregate mean (9000.0) is reported; individual raw readings are not.
    assert "8000" not in text and "10000" not in text


@pytest.mark.asyncio
async def test_health_summary_handles_empty_store():
    agent = _make(HealthAgent)
    with _patch_graph({"HealthRecord": []}):
        text, _ = await agent._sleep_summary(MagicMock())
    assert "no recent sleep data" in text


@pytest.mark.asyncio
async def test_health_medication_reports_count_not_names():
    agent = _make(HealthAgent)
    meds = [{"category": "medication", "name": "SecretDrug"}]
    with _patch_graph({"HealthRecord": meds}):
        text, _ = await agent._medication_reminder(MagicMock())
    assert "1 scheduled item" in text
    assert "SecretDrug" not in text


@pytest.mark.asyncio
async def test_financial_check_budget_reports_remaining():
    agent = _make(FinancialAgent)
    nodes = [
        {"kind": "budget", "amount": 100},
        {"kind": "spend", "amount": 30},
        {"kind": "transaction", "amount": 10},
    ]
    with _patch_graph({"Financial": nodes}):
        text, _ = await agent._check_budget(MagicMock())

    assert "pending" not in text.lower()
    assert "within budget" in text
    assert "60.0" in text  # 100 - 40 remaining


@pytest.mark.asyncio
async def test_financial_check_budget_empty():
    agent = _make(FinancialAgent)
    with _patch_graph({"Financial": []}):
        text, _ = await agent._check_budget(MagicMock())
    assert "no budget data" in text


@pytest.mark.asyncio
async def test_personal_daily_briefing_reads_news_and_tasks():
    agent = _make(PersonalAgent, mcp=None)  # no calendar source
    data = {
        "News": [{"title": "Big AI news"}],
        "Task": [{"title": "Ship the PR"}],
    }
    with _patch_graph(data):
        text, _ = await agent._daily_briefing(MagicMock())

    assert "Phase 3" not in text
    assert "Big AI news" in text
    assert "Ship the PR" in text
    assert "calendar source unavailable" in text.lower()


@pytest.mark.asyncio
async def test_personal_daily_briefing_includes_calendar():
    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        return_value=MagicMock(
            success=True,
            output=[{"summary": "Standup", "start": {"dateTime": "2026-06-21T09:00"}}],
        )
    )
    agent = _make(PersonalAgent, mcp=mcp)
    with _patch_graph({"News": [], "Task": []}):
        text, _ = await agent._daily_briefing(MagicMock())

    assert "Standup" in text
    mcp.invoke.assert_awaited_once_with("gcal", "list_events", {"max_results": 5})
