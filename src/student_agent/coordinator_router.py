"""Case-scoped A2A coordination; messages are internal, not output-schema fields."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .trace import TraceWriter

Actor = Literal[
    "entity-agent",
    "order-agent",
    "shipment-agent",
    "payment-agent",
    "policy-agent",
    "verifier",
]
Status = Literal["requested", "accepted", "completed", "failed", "timed_out"]


@dataclass(frozen=True)
class TaskMessage:
    message_id: str
    case_id: str
    correlation_id: str
    causation_id: str | None
    sender: str
    target: Actor
    task: str
    entity_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    claim_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)
    attempt: int = 1
    deadline_ms: int = 30_000
    status: Status = "requested"

    def __post_init__(self) -> None:
        if not self.message_id or not self.case_id or self.correlation_id != self.case_id:
            raise ValueError("A2A message must have an ID and matching case correlation")
        if self.sender != "coordinator":
            raise ValueError("only coordinator may assign specialist tasks")
        if not self.task or not 1 <= self.attempt <= 2 or self.deadline_ms <= 0:
            raise ValueError("invalid A2A task, attempt, or deadline")
        if self.status != "requested":
            raise ValueError("a newly assigned task must be requested")


@dataclass(frozen=True)
class TaskResult:
    case_id: str
    message_id: str
    actor: Actor
    status: Literal["completed", "failed", "timed_out"]
    facts: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    decision_code: str | None = None


TaskHandler = Callable[[TaskMessage], Awaitable[TaskResult]]


class Coordinator:
    """Owns task routing, handoff limits and observable trace for one case."""

    def __init__(
        self,
        case_id: str,
        trace: TraceWriter,
        handlers: Mapping[Actor, TaskHandler],
        *,
        max_handoffs: int = 12,
        concurrency: int = 3,
    ) -> None:
        if max_handoffs <= 0 or concurrency <= 0:
            raise ValueError("handoff and concurrency limits must be positive")
        self.case_id = case_id
        self.trace = trace
        self.handlers = dict(handlers)
        self.max_handoffs = max_handoffs
        self._handoffs = 0
        self._seen_messages: set[str] = set()
        self._task_attempts: dict[tuple[str, str], int] = {}
        self._semaphore = asyncio.Semaphore(concurrency)

    async def assign(
        self,
        target: Actor,
        task: str,
        *,
        entity_ids: Mapping[str, tuple[str, ...]] | None = None,
        claim_ids: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        facts: Mapping[str, Any] | None = None,
        causation_id: str | None = None,
        deadline_ms: int = 30_000,
    ) -> TaskResult:
        if target not in self.handlers:
            raise ValueError(f"no handler registered for {target}")
        if self._handoffs >= self.max_handoffs:
            raise ValueError("case handoff budget exhausted")
        canonical_scope = repr((target, task, sorted((entity_ids or {}).items()), claim_ids))
        key = (target, canonical_scope)
        attempts = self._task_attempts.get(key, 0) + 1
        if attempts > 2:
            raise ValueError("task attempt budget exhausted")
        self._task_attempts[key] = attempts
        self._handoffs += 1
        message = TaskMessage(
            message_id=f"msg_{secrets.token_urlsafe(18)}",
            case_id=self.case_id,
            correlation_id=self.case_id,
            causation_id=causation_id,
            sender="coordinator",
            target=target,
            task=task,
            entity_ids=entity_ids or {},
            claim_ids=claim_ids,
            evidence_refs=evidence_refs,
            facts=facts or {},
            attempt=attempts,
            deadline_ms=deadline_ms,
        )
        if message.message_id in self._seen_messages:
            raise ValueError("duplicate A2A message ID")
        self._seen_messages.add(message.message_id)
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            decision_code=task,
            attributes={"attempt": attempts, "message_id": message.message_id},
        )
        try:
            async with self._semaphore:
                result = await asyncio.wait_for(
                    self.handlers[target](message), timeout=deadline_ms / 1000
                )
        except TimeoutError:
            result = TaskResult(
                self.case_id, message.message_id, target, "timed_out", decision_code="TASK_TIMEOUT"
            )
        except Exception:
            result = TaskResult(
                self.case_id, message.message_id, target, "failed", decision_code="TASK_FAILED"
            )
        if result.case_id != self.case_id or result.message_id != message.message_id:
            raise ValueError("specialist result has mismatched case or message ID")
        if result.actor != target or result.status not in {"completed", "failed", "timed_out"}:
            raise ValueError("specialist result has invalid actor or status")
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=target,
            target="coordinator",
            decision_code=result.decision_code or result.status.upper(),
            evidence_refs=list(result.evidence_refs),
            attributes={"message_id": message.message_id, "attempt": attempts},
        )
        return result
