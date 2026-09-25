from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .cases import CASE_ID_PATTERN
from .contracts import Contracts


@dataclass(frozen=True)
class DiscoveredTool:
    name: str
    input_schema: dict[str, Any]


class ToolExecutionError(RuntimeError):
    """A tool reported an execution failure, not an empty evidence result."""


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._discovered: list[DiscoveredTool] | None = None

    async def list_tools(self) -> list[str]:
        return [tool.name for tool in await self.describe_tools()]

    async def describe_tools(self) -> list[DiscoveredTool]:
        """Return server-declared tool names and input schemas for reviewed binding."""
        if self._discovered is not None:
            return list(self._discovered)
        response = await self._session.list_tools()
        tools = (DiscoveredTool(tool.name, dict(tool.input_schema)) for tool in response.tools)
        self._discovered = sorted(tools, key=lambda tool: tool.name)
        return list(self._discovered)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise ValueError("MCP call requires a valid case_id")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("MCP call requires a non-empty tool name")
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise ToolExecutionError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = result.structured_content
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
