
import pytest
from unittest.mock import AsyncMock, MagicMock
from master.agents.librarian.graph_client import GraphClient
from master.agents.librarian.access_control import NodeType, RelationType

@pytest.fixture
def mock_driver():
    driver = MagicMock()
    session = AsyncMock()
    driver.session.return_value.__aenter__.return_value = session
    return driver, session

@pytest.mark.asyncio
async def test_upsert_node_valid_type(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)

    await client.upsert_node(NodeType.PERSON.value, "123", {"name": "test"})

    session.run.assert_called_once()
    query = session.run.call_args[0][0]
    assert f"MERGE (n:{NodeType.PERSON.value}" in query

@pytest.mark.asyncio
async def test_upsert_node_invalid_type(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)

    with pytest.raises(ValueError, match="Unauthorized graph identifier"):
        await client.upsert_node("InvalidType", "123", {"name": "test"})

    session.run.assert_not_called()

@pytest.mark.asyncio
async def test_upsert_node_malicious_type(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)

    malicious = "Person) DETACH DELETE n; MERGE (m:Hacked"
    with pytest.raises(ValueError, match="Invalid characters in identifier"):
        await client.upsert_node(malicious, "123", {"name": "test"})

    session.run.assert_not_called()

@pytest.mark.asyncio
async def test_upsert_edge_valid_relation(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)

    await client.upsert_edge("a", "b", RelationType.WORKS_WITH.value)

    session.run.assert_called_once()
    query = session.run.call_args[0][0]
    assert f"MERGE (a)-[r:{RelationType.WORKS_WITH.value}]->(b)" in query

@pytest.mark.asyncio
async def test_upsert_edge_invalid_relation(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)

    with pytest.raises(ValueError, match="Unauthorized graph identifier"):
        await client.upsert_edge("a", "b", "INVALID_RELATION")

    session.run.assert_not_called()

@pytest.mark.asyncio
async def test_vector_search_valid_types(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)
    session.run.return_value.data.return_value = []

    await client.vector_search([0.1]*1536, node_types=[NodeType.PERSON.value, NodeType.PLACE.value])

    session.run.assert_called_once()
    query = session.run.call_args[0][0]
    kwargs = session.run.call_args[1]
    assert "$node_types" in query
    assert kwargs["node_types"] == [NodeType.PERSON.value, NodeType.PLACE.value]

@pytest.mark.asyncio
async def test_vector_search_invalid_types(mock_driver):
    driver, session = mock_driver
    client = GraphClient(driver)

    with pytest.raises(ValueError, match="Unauthorized graph identifier"):
        await client.vector_search([0.1]*1536, node_types=[NodeType.PERSON.value, "InvalidType"])

    session.run.assert_not_called()
