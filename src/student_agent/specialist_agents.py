"""Small specialist boundary for fetching and explicitly consuming MCP evidence."""

from __future__ import annotations

import copy
from typing import Any

from .coordinator_router import Actor
from .mcp_evidence_collector import ScopedEvidence


class EvidenceObservation:
    """An opaque server evidence envelope returned to one specialist.

    The evidence reference is read-only and ``data`` returns a copy.  This keeps the
    server-issued provenance fields separate from mutable business analysis.
    """

    def __init__(self, tool_name: str, envelope: dict[str, Any]) -> None:
        self._tool_name = tool_name
        self._envelope = copy.deepcopy(envelope)

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def evidence_ref(self) -> str:
        return self._envelope["evidence_ref"]

    @property
    def domain(self) -> str:
        return self._envelope["domain"]

    @property
    def data(self) -> Any:
        return copy.deepcopy(self._envelope["data"])


class SpecialistAgent:
    """Case-scoped MCP facade shared by concrete specialist handlers."""

    def __init__(self, actor: Actor, evidence: ScopedEvidence) -> None:
        if actor in {"verifier"}:
            raise ValueError(f"{actor} is not allowed to query evidence")
        self.actor = actor
        self._evidence = evidence

    async def fetch(self, tool_name: str, **arguments: str) -> EvidenceObservation:
        envelope = await self._evidence.call(self.actor, tool_name, **arguments)
        return EvidenceObservation(tool_name, envelope)

    def use(self, observation: EvidenceObservation) -> Any:
        """Record tool_result_consumed and return a copy of authoritative data."""
        self._evidence.consume(self.actor, observation.tool_name, observation.evidence_ref)
        return observation.data
