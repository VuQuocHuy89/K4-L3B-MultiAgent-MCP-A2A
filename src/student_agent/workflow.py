from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .case_adapter import CATALOG, build_output, make_handlers
from .contracts import Contracts
from .coordinator_router import Actor, Coordinator, TaskHandler, TaskResult
from .mcp_evidence_collector import ScopedEvidence
from .policy_agent import decide_policy
from .trace import TraceWriter
from .verifier_agent import verify_output

if TYPE_CHECKING:
    from .mcp_gateway import EvidenceGateway


OutputBuilder = Callable[[dict[str, Any], Mapping[str, TaskResult]], dict[str, Any]]


def _task_deadline_ms(evidence: ScopedEvidence, sequential_calls: int) -> int:
    """Allow two scoped attempts per call, including retry delay and cleanup."""
    seconds = sequential_calls * (2 * evidence.timeout_seconds + evidence.retry_delay_seconds) + 5
    return int(seconds * 1000)


async def run_case_with_handlers(
    case: dict[str, Any],
    *,
    handlers: Mapping[Actor, TaskHandler],
    build_output: OutputBuilder,
    evidence: ScopedEvidence,
    contracts: Contracts,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Run the fixed A2A route once case-specific handlers and mapping are supplied."""
    case_id = case["case_id"]
    if evidence.case_id != case_id:
        raise ValueError("evidence context belongs to a different case")
    coordinator = Coordinator(case_id, trace, handlers)
    results: dict[str, TaskResult] = {}
    # Entity resolution may make two sequential MCP calls.
    entity = await coordinator.assign(
        "entity-agent",
        "resolve_entity",
        deadline_ms=_task_deadline_ms(evidence, 2),
    )
    if entity.status != "completed":
        raise ValueError("entity agent did not complete")
    if not set(entity.evidence_refs) <= evidence.consumed_by("entity-agent"):
        raise ValueError("entity handoff cites evidence not consumed in this case")
    results["entity"] = entity
    if entity.facts.get("status") == "resolved":
        tasks = (
            ("order", "order-agent", "analyse_order"),
            ("shipment", "shipment-agent", "analyse_shipment"),
            ("payment", "payment-agent", "analyse_payment"),
        )
        specialist_results = await asyncio.gather(
            *(
                coordinator.assign(
                    actor,
                    task,
                    causation_id=entity.message_id,
                    facts=entity.facts,
                    deadline_ms=_task_deadline_ms(evidence, 2),
                )
                for _, actor, task in tasks
            )
        )
        for (name, actor, _), result in zip(tasks, specialist_results, strict=True):
            if result.status != "completed":
                raise ValueError(f"{name} specialist did not complete")
            if not set(result.evidence_refs) <= evidence.consumed_by(actor):
                raise ValueError(f"{name} handoff cites evidence not consumed in this case")
            results[name] = result
        policy_inputs: dict[str, Any] = {}
        for result in (entity, *specialist_results):
            policy_inputs.update(result.facts)
        policy = await coordinator.assign(
            "policy-agent",
            "resolve_policy_and_conflicts",
            evidence_refs=tuple(
                sorted({ref for result in specialist_results for ref in result.evidence_refs})
            ),
            facts=policy_inputs,
            deadline_ms=_task_deadline_ms(evidence, 1),
        )
        if policy.status != "completed":
            raise ValueError("policy agent did not complete")
        if not set(policy.evidence_refs) <= evidence.consumed_by("policy-agent"):
            raise ValueError("policy handoff cites evidence not consumed in this case")
        normalized_facts = {**policy_inputs, **policy.facts}
        policy = replace(policy, facts={**policy.facts, **decide_policy(normalized_facts)})
        results["policy"] = policy
    elif entity.facts.get("status") not in {"ambiguous", "not_found"}:
        raise ValueError("entity agent returned an invalid status")
    output = build_output(case, results)
    decision_refs = sorted({ref for result in results.values() for ref in result.evidence_refs})[
        :20
    ]
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=decision_refs,
    )
    verify_output(
        output,
        case_id=case_id,
        consumed_refs=evidence.consumed_refs,
        contracts=contracts,
        trace=trace,
        require_lifecycle=True,
        evidence_domains=evidence.evidence_domains,
    )
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the reviewed, case-scoped evidence route with conservative decisions."""
    discovered = {tool.name: tool for tool in await gateway.describe_tools()}
    for name, spec in CATALOG.items():
        tool = discovered.get(name)
        expected = spec.required_arguments | {"case_id"}
        if tool is None or set(tool.input_schema.get("required", [])) != expected:
            raise RuntimeError(f"MCP tool {name} is missing or has an unexpected input schema")
    evidence = ScopedEvidence(
        case_id=case["case_id"],
        gateway=gateway,
        contracts=trace.contracts,
        trace=trace,
        catalog=CATALOG,
        discovered_tools=set(discovered),
    )
    return await run_case_with_handlers(
        case,
        handlers=make_handlers(case, evidence),
        build_output=build_output,
        evidence=evidence,
        contracts=trace.contracts,
        trace=trace,
    )
