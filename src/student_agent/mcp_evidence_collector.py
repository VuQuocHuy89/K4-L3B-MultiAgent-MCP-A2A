"""Tool permissions, case-local cache, call budget, and bounded MCP retry."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

from .cases import CASE_ID_PATTERN
from .contracts import Contracts
from .trace import TraceWriter


class Gateway(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]: ...


DOMAIN_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"order", "customer"}),
    "order-agent": frozenset({"order", "item", "product", "seller"}),
    "shipment-agent": frozenset({"shipment"}),
    "payment-agent": frozenset({"payment", "refund"}),
    "policy-agent": frozenset({"policy"}),
    "verifier": frozenset(),
    "coordinator": frozenset(),
}


@dataclass(frozen=True)
class ToolSpec:
    domain: str
    required_arguments: frozenset[str]
    allowed_actors: frozenset[str]

    def __post_init__(self) -> None:
        if "case_id" in self.required_arguments:
            raise ValueError("case_id is supplied by the scoped gateway, not tool arguments")
        if not self.allowed_actors or not self.allowed_actors <= DOMAIN_PERMISSIONS.keys():
            raise ValueError("tool spec contains unknown or empty actor permission")
        if any(self.domain not in DOMAIN_PERMISSIONS[actor] for actor in self.allowed_actors):
            raise ValueError("tool spec grants a domain outside the actor permission matrix")


class CallBudgetExceeded(RuntimeError):
    pass


class ScopedEvidence:
    """One instance per case; only reviewed and discovered tools can be called."""

    def __init__(
        self,
        *,
        case_id: str,
        gateway: Gateway,
        contracts: Contracts,
        trace: TraceWriter,
        catalog: Mapping[str, ToolSpec],
        discovered_tools: set[str],
        call_limit: int = 12,
        timeout_seconds: float = 30,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if call_limit <= 0 or timeout_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("call limits and timeout must be positive")
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise ValueError("invalid case_id for scoped evidence")
        self.case_id = case_id
        self.gateway = gateway
        self.contracts = contracts
        self.trace = trace
        self.catalog = dict(catalog)
        self.discovered_tools = set(discovered_tools)
        self.call_limit = call_limit
        self.timeout_seconds = timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.attempted_calls = 0
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._by_ref: dict[str, dict[str, Any]] = {}
        self._tool_by_ref: dict[str, str] = {}
        self._callers_by_ref: dict[str, set[str]] = {}
        self._consumed: set[tuple[str, str]] = set()
        self._budget_lock = asyncio.Lock()
        self._key_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def call(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        spec = self.catalog.get(tool_name)
        if spec is None or tool_name not in self.discovered_tools:
            raise PermissionError(f"tool {tool_name} is not reviewed and discovered")
        if actor not in spec.allowed_actors:
            raise PermissionError(f"{actor} cannot call {tool_name}")
        if set(arguments) != spec.required_arguments:
            raise ValueError(f"{tool_name} requires arguments {sorted(spec.required_arguments)}")
        if any(not isinstance(value, str) or not value for value in arguments.values()):
            raise ValueError("tool arguments must be nonempty strings")
        key = (tool_name, json.dumps(arguments, sort_keys=True, separators=(",", ":")))
        key_lock = self._key_locks.setdefault(key, asyncio.Lock())
        async with key_lock:
            if key in self._cache:
                evidence = self._cache[key]
                self._callers_by_ref[evidence["evidence_ref"]].add(actor)
                return copy.deepcopy(evidence)
            for attempt in (1, 2):
                async with self._budget_lock:
                    if self.attempted_calls >= self.call_limit:
                        raise CallBudgetExceeded("MCP call budget exhausted")
                    self.attempted_calls += 1
                try:
                    evidence = await asyncio.wait_for(
                        self.gateway.call(tool_name, case_id=self.case_id, **arguments),
                        timeout=self.timeout_seconds,
                    )
                    self.contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
                    if evidence["domain"] != spec.domain:
                        raise ValueError("MCP evidence domain does not match reviewed tool")
                    ref = evidence["evidence_ref"]
                    if ref in self._by_ref and self._by_ref[ref] != evidence:
                        raise ValueError("MCP evidence reference was reused with different content")
                    if ref in self._tool_by_ref and self._tool_by_ref[ref] != tool_name:
                        raise ValueError("MCP evidence reference was reused by a different tool")
                    # Keep an authoritative private copy of the server envelope.  Agent
                    # code receives another copy so it cannot mutate evidence_ref/hash in
                    # the case-local index, accidentally or otherwise.
                    stored = copy.deepcopy(evidence)
                    self._by_ref[ref] = stored
                    self._tool_by_ref[ref] = tool_name
                    self._callers_by_ref.setdefault(ref, set()).add(actor)
                    self._cache[key] = stored
                    return copy.deepcopy(stored)
                except (TimeoutError, ConnectionError, httpx2.RequestError):
                    if attempt == 2 or self.attempted_calls >= self.call_limit:
                        raise
                    await asyncio.sleep(self.retry_delay_seconds)
                except httpx2.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if (
                        attempt == 2
                        or self.attempted_calls >= self.call_limit
                        or (status not in {408, 429} and status < 500)
                    ):
                        raise
                    await asyncio.sleep(self.retry_delay_seconds)
        raise AssertionError("unreachable retry state")

    def consume(self, actor: str, tool_name: str, evidence_ref: str) -> None:
        spec = self.catalog.get(tool_name)
        evidence = self._by_ref.get(evidence_ref)
        if (
            spec is None
            or actor not in spec.allowed_actors
            or evidence is None
            or self._tool_by_ref.get(evidence_ref) != tool_name
            or actor not in self._callers_by_ref.get(evidence_ref, set())
        ):
            raise ValueError("evidence was not obtained by an allowed actor in this case")
        if evidence["domain"] != spec.domain:
            raise ValueError("evidence domain does not match tool")
        consumed_key = (actor, evidence_ref)
        if consumed_key in self._consumed:
            return
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        self._consumed.add(consumed_key)

    def consumed_by(self, actor: str) -> frozenset[str]:
        """Return refs for which this actor emitted tool_result_consumed."""
        return frozenset(ref for consumed_actor, ref in self._consumed if consumed_actor == actor)

    @property
    def consumed_refs(self) -> frozenset[str]:
        return frozenset(ref for _, ref in self._consumed)

    @property
    def evidence_domains(self) -> dict[str, str]:
        return {ref: evidence["domain"] for ref, evidence in self._by_ref.items()}
