"""Reviewed MCP bindings with temporal entity resolution and per-claim citations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .coordinator_router import Actor, TaskHandler, TaskMessage, TaskResult
from .domain_analysis import (
    analyze_items,
    analyze_payment,
    analyze_refund,
    analyze_shipment,
    ids,
    reconcile_ledger,
    records,
    select_order,
)
from .mcp_evidence_collector import ScopedEvidence, ToolSpec
from .mcp_gateway import ToolExecutionError
from .policy_agent import decide_policy
from .specialist_agents import SpecialistAgent


def _spec(domain: str, argument: str, actor: str) -> ToolSpec:
    return ToolSpec(domain, frozenset({argument}), frozenset({actor}))


CATALOG = {
    "get_order": _spec("order", "order_id", "entity-agent"),
    "get_customer_history": _spec("customer", "customer_unique_id", "entity-agent"),
    "get_order_items": _spec("item", "order_id", "order-agent"),
    "get_product_context": _spec("product", "order_id", "order-agent"),
    "get_shipment_summary": _spec("shipment", "order_id", "shipment-agent"),
    "get_payment_timeline": _spec("payment", "order_id", "payment-agent"),
    "get_refund_timeline": _spec("refund", "order_id", "payment-agent"),
    "get_policy": _spec("policy", "policy_version", "policy-agent"),
}


def _result(message: TaskMessage, facts: Mapping[str, Any], refs: list[str]) -> TaskResult:
    return TaskResult(
        message.case_id, message.message_id, message.target, "completed", facts, tuple(refs)
    )


def make_handlers(case: dict[str, Any], evidence: ScopedEvidence) -> Mapping[Actor, TaskHandler]:
    async def fetch(agent: SpecialistAgent, tool: str, refs: list[str], **arguments: str) -> Any:
        observation = await agent.fetch(tool, **arguments)
        data = agent.use(observation)
        refs.append(observation.evidence_ref)
        return data

    async def entity(message: TaskMessage) -> TaskResult:
        agent, refs = SpecialistAgent("entity-agent", evidence), []
        request = case.get("customer_request", {})
        candidates = ids([request.get("claimed_order_id"), *case.get("candidate_order_ids", [])])
        facts: dict[str, Any] = dict(
            status="not_found",
            resolved_order_ids=[],
            rejected_candidates=[],
            entity_confidence=0.2,
            customer_unique_id=None,
            related_order_ids=[],
            entity_conflicts=[],
        )
        hint = case.get("customer_unique_id_hint")
        history = []
        if isinstance(hint, str) and hint:
            data = await fetch(agent, "get_customer_history", refs, customer_unique_id=hint)
            if isinstance(data, dict) and data.get("customer_unique_id") == hint:
                history = records(data.get("orders"))
                facts.update(
                    customer_unique_id=hint,
                    related_order_ids=ids(row.get("order_id") for row in history),
                )
        matching = [
            candidate for candidate in candidates if candidate in facts["related_order_ids"]
        ]
        if len(matching) > 1:
            facts.update(status="ambiguous", entity_confidence=0.4)
            return _result(message, facts, refs)
        claimed = request.get("claimed_order_id")
        chosen = matching[0] if matching else claimed if isinstance(claimed, str) else None
        if chosen:
            direct = await fetch(agent, "get_order", refs, order_id=chosen)
            if not isinstance(direct, dict) or direct.get("order_id") != chosen:
                return _result(message, facts, refs)
            selected, end, conflicts = select_order(direct, history, case.get("opened_at"))
            facts["entity_conflicts"] = conflicts
            if not selected:
                facts.update(status="ambiguous", entity_confidence=0.4)
                return _result(message, facts, refs)
            facts.update(
                status="resolved",
                resolved_order_ids=[chosen],
                rejected_candidates=[c for c in candidates if c not in matching]
                if matching
                else [],
                entity_confidence=0.98 if matching else 0.85,
                order_id=chosen,
                order_record=selected,
                order_status=selected.get("order_status"),
                period_start=selected.get("order_purchase_timestamp"),
                period_end=end,
            )
        return _result(message, facts, refs)

    async def order(message: TaskMessage) -> TaskResult:
        agent, refs = SpecialistAgent("order-agent", evidence), []
        scope = dict(message.facts)
        data = await fetch(agent, "get_order_items", refs, order_id=scope["order_id"])
        facts = analyze_items(records(data), scope)
        if case.get("investigation_scope", {}).get("include_product_context"):
            products = records(
                await fetch(agent, "get_product_context", refs, order_id=scope["order_id"])
            )
            facts["product_context_verified"] = bool(facts["item_ids"]) and all(
                any(p.get("order_item_id") == item for p in products) for item in facts["item_ids"]
            )
        return _result(message, facts, refs)

    async def shipment(message: TaskMessage) -> TaskResult:
        agent, refs = SpecialistAgent("shipment-agent", evidence), []
        scope = dict(message.facts)
        data = await fetch(agent, "get_shipment_summary", refs, order_id=scope["order_id"])
        if not isinstance(data, dict) or data.get("order_id") != scope["order_id"]:
            raise ValueError("shipment evidence does not match resolved order")
        return _result(message, analyze_shipment(data, scope), refs)

    async def payment(message: TaskMessage) -> TaskResult:
        agent, refs = SpecialistAgent("payment-agent", evidence), []
        scope = dict(message.facts)
        data = await fetch(agent, "get_payment_timeline", refs, order_id=scope["order_id"])
        if not isinstance(data, dict) or data.get("order_id") != scope["order_id"]:
            raise ValueError("payment evidence does not match resolved order")
        facts = analyze_payment(data, scope)
        facts.update(refunded_total_brl=None, refund_checked=False)
        topics = {c.get("topic") for c in case.get("customer_request", {}).get("claims", [])}
        if topics & {"refund_pending", "refund_failed"}:
            try:
                refund = await fetch(agent, "get_refund_timeline", refs, order_id=scope["order_id"])
            except ToolExecutionError:
                facts["refund_unavailable"] = True
            else:
                if not isinstance(refund, dict) or refund.get("order_id") != scope["order_id"]:
                    raise ValueError("refund evidence does not match resolved order")
                facts.update(analyze_refund(refund, scope))
        return _result(message, facts, refs)

    async def policy(message: TaskMessage) -> TaskResult:
        agent, refs = SpecialistAgent("policy-agent", evidence), []
        data = await fetch(agent, "get_policy", refs, policy_version=case["policy_version"])
        if not isinstance(data, dict) or data.get("policy_version") != case["policy_version"]:
            raise ValueError("policy version mismatch")
        facts = dict(message.facts)
        if not facts.get("explicit_mismatch") and not facts.get("explicit_duplicate"):
            facts.update(reconcile_ledger(facts))
        paid, total = facts.get("captured_total_brl"), facts.get("order_total_brl")
        if facts.get("payment_ambiguous"):
            verdict = "insufficient_evidence"
        elif facts.get("refund_verdict"):
            verdict = facts["refund_verdict"]
        elif facts.get("explicit_duplicate"):
            verdict = "duplicate_capture"
        elif facts.get("explicit_mismatch"):
            verdict = "capture_mismatch"
        elif paid is not None and total is not None and abs(paid - total) < 0.005:
            verdict = "reconciled"
        elif (
            paid is not None
            and total is not None
            and paid > total
            and facts.get("capture_count", 0) > 1
        ):
            verdict = "duplicate_capture"
        else:
            verdict = "insufficient_evidence"
        facts["payment_verdict"] = verdict
        facts["is_valid_split_payment"] = (
            verdict == "reconciled" and facts.get("capture_count", 0) > 1
        )
        topics = {c.get("topic") for c in case.get("customer_request", {}).get("claims", [])}
        facts["claim_disproved"] = (
            verdict == "reconciled"
            and facts.get("shipment_verdict") == "on_time"
            and bool(
                topics
                & {
                    "unsupported_claim",
                    "late_delivery_seller",
                    "late_delivery_logistics",
                    "duplicate_charge",
                }
            )
        )
        conflicts = (
            facts.get("entity_conflicts", [])
            + facts.get("shipment_conflicts", [])
            + facts.get("payment_conflicts", [])
        )
        facts.update(
            policy_rules=data.get("rules", {}),
            has_unresolved_conflict=any(c["selected_source"] is None for c in conflicts),
            has_resolved_conflict=bool(conflicts),
            evidence_coverage=0.98
            if paid is not None and facts.get("order_items_reliable")
            else 0.7,
            analysis_complete=paid is not None and facts.get("order_items_reliable", False),
        )
        if facts.get("refund_unavailable"):
            facts.update(evidence_coverage=0.4, analysis_complete=False)
        return _result(message, facts, refs)

    return {
        "entity-agent": entity,
        "order-agent": order,
        "shipment-agent": shipment,
        "payment-agent": payment,
        "policy-agent": policy,
    }


def build_output(case: dict[str, Any], results: Mapping[str, TaskResult]) -> dict[str, Any]:
    entity = dict(results["entity"].facts)
    facts: dict[str, Any] = {}
    for result in results.values():
        facts.update(result.facts)
    keys = ("assessment", "root_cause_analysis", "financial_resolution", "resolution_actions")
    decision = {key: facts[key] for key in keys} if "assessment" in facts else decide_policy(facts)
    refs = sorted({ref for result in results.values() for ref in result.evidence_refs})
    issue, confidence = (
        decision["assessment"]["primary_issue"],
        decision["assessment"]["confidence"],
    )
    claims = []
    for claim in case.get("customer_request", {}).get("claims", [])[:5]:
        topic = claim.get("topic")
        relevant = {"entity", "policy", "order"} | (
            {"shipment"}
            if topic in {"late_delivery_seller", "late_delivery_logistics"}
            else {"payment"}
        )
        verdict = "insufficient_evidence"
        if issue != "insufficient_evidence":
            if topic == "requested_full_refund":
                amount, paid = (
                    decision["financial_resolution"]["recommended_refund_brl"],
                    facts.get("order_total_brl") or facts.get("captured_total_brl"),
                )
                verdict = (
                    "unsupported"
                    if amount == 0
                    else "supported"
                    if paid is not None and amount >= paid
                    else "partially_supported"
                )
            elif topic == issue or topic in decision["assessment"]["secondary_issues"]:
                verdict = "supported"
            elif (
                topic in {"late_delivery_seller", "late_delivery_logistics"}
                and facts.get("shipment_verdict") == "on_time"
                or topic in {"duplicate_charge", "payment_mismatch"}
                and facts.get("payment_verdict") == "reconciled"
            ):
                verdict = "unsupported"
        if topic == "unsupported_claim":
            relevant.add("shipment")
        claim_refs = sorted(
            {ref for name in relevant if name in results for ref in results[name].evidence_refs}
        )
        claims.append(
            dict(
                claim_id=claim["claim_id"],
                verdict=verdict,
                confidence=min(confidence, 0.5)
                if verdict == "insufficient_evidence"
                else confidence,
                evidence_refs=claim_refs,
            )
        )
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        **decision,
        "affected_entities": dict(
            order_ids=entity["resolved_order_ids"],
            item_ids=facts.get("item_ids", []),
            seller_ids=facts.get("seller_ids", []),
            payment_references=facts.get("payment_references", []),
            shipment_ids=facts.get("shipment_ids", []),
        ),
        "entity_resolution": dict(
            status=entity["status"],
            resolved_order_ids=entity["resolved_order_ids"],
            rejected_candidates=entity["rejected_candidates"],
            confidence=entity["entity_confidence"],
        ),
        "customer_context": dict(
            customer_unique_id=entity["customer_unique_id"],
            related_order_ids=entity["related_order_ids"],
        ),
        "shipment_analysis": dict(
            verdict=facts.get("shipment_verdict", "insufficient_evidence"),
            late_seller_ids=facts.get("late_seller_ids", []),
            timeline_complete=facts.get("timeline_complete", False),
        ),
        "payment_analysis": dict(
            verdict=facts.get("payment_verdict", "insufficient_evidence"),
            captured_total_brl=facts.get("captured_total_brl"),
            refunded_total_brl=facts.get("refunded_total_brl"),
            refundable_total_brl=decision["financial_resolution"]["recommended_refund_brl"]
            if issue != "insufficient_evidence"
            else None,
        ),
        "claim_assessments": claims,
        "evidence_refs": refs,
        "data_conflicts": (
            facts.get("entity_conflicts", [])
            + facts.get("shipment_conflicts", [])
            + facts.get("payment_conflicts", [])
        )[:5],
    }
