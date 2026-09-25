"""Checks output invariants that do not require private business oracles."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .contracts import Contracts
from .trace import TraceWriter


class VerificationError(ValueError):
    pass


_ISSUE_PARTIES = {
    "canceled_order_paid": {"seller", "platform"},
    "unavailable_order_paid": {"seller", "platform"},
    "late_delivery_seller": {"seller"},
    "late_delivery_logistics": {"logistics_provider"},
    "payment_mismatch": {"payment_provider", "platform"},
    "duplicate_charge": {"payment_provider", "platform"},
    "refund_pending": {"payment_provider", "platform"},
    "refund_failed": {"payment_provider", "platform"},
    "valid_split_payment": {"customer"},
    "unsupported_claim": {"customer"},
    "insufficient_evidence": {"unknown"},
}
_FINANCIAL_ISSUES = {
    "late_delivery_seller",
    "late_delivery_logistics",
    "canceled_order_paid",
    "unavailable_order_paid",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}


def check_evidence_coverage(output: dict[str, Any], domains: dict[str, str]) -> None:
    """Necessary domain checks; private scorer requirements may be stricter."""
    refs = set(output["evidence_refs"])
    if not refs <= domains.keys():
        raise VerificationError("output has evidence not linked to a known MCP tool")
    required = set()
    issue = output["assessment"]["primary_issue"]
    if output["entity_resolution"]["status"] == "resolved":
        required.add("order")
    if output["customer_context"]["customer_unique_id"] is not None:
        required.add("customer")
    if issue != "insufficient_evidence":
        required.update({"order", "policy"})
    if issue.startswith("late_delivery"):
        required.add("shipment")
    if issue in _FINANCIAL_ISSUES or issue == "valid_split_payment":
        required.add("payment")
    if issue in {"refund_failed", "refund_pending"}:
        required.add("refund")
    missing = required - {domains[ref] for ref in refs}
    if missing:
        raise VerificationError(f"missing required evidence domains: {sorted(missing)}")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= refs:
            raise VerificationError("claim evidence is outside case output evidence")


def _verify_lifecycle(trace: TraceWriter, case_id: str) -> None:
    required = [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
    ]
    if not trace.path.exists():
        raise VerificationError("trace lifecycle is missing")
    events = [
        json.loads(line)
        for line in trace.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_types = [event["event_type"] for event in events if event.get("case_id") == case_id]
    position = -1
    for event_type in required:
        try:
            position = event_types.index(event_type, position + 1)
        except ValueError as exc:
            message = f"trace lifecycle missing or out of order: {event_type}"
            raise VerificationError(message) from exc


def verify_output(
    output: dict[str, Any],
    *,
    case_id: str,
    consumed_refs: frozenset[str],
    contracts: Contracts,
    trace: TraceWriter | None,
    require_lifecycle: bool = False,
    evidence_domains: dict[str, str] | None = None,
) -> None:
    """Validate public shape and decidable consistency before the CLI writes output."""
    try:
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output["case_id"] != case_id:
            raise VerificationError("output case_id differs from input")
        if require_lifecycle:
            if trace is None:
                raise VerificationError("trace is required for lifecycle verification")
            _verify_lifecycle(trace, case_id)
        refs = set(output["evidence_refs"])
        if evidence_domains is not None:
            check_evidence_coverage(output, evidence_domains)
        if not refs <= consumed_refs:
            raise VerificationError("output cites evidence not consumed in this case")
        resolution = output["entity_resolution"]
        resolved = set(resolution["resolved_order_ids"])
        rejected = set(resolution["rejected_candidates"])
        if resolved & rejected:
            raise VerificationError("resolved and rejected order IDs overlap")
        if rejected & set(output["affected_entities"]["order_ids"]):
            raise VerificationError("rejected order appears in affected entities")
        if resolution["status"] != "resolved" and resolved:
            raise VerificationError("unresolved entity has resolved order IDs")
        if resolution["status"] != "resolved" and output["assessment"]["confidence"] > 0.5:
            raise VerificationError("unresolved entity confidence exceeds 0.5")
        if not set(output["shipment_analysis"]["late_seller_ids"]) <= set(
            output["affected_entities"]["seller_ids"]
        ):
            raise VerificationError("late seller is outside affected sellers")
        for claim in output.get("claim_assessments", []):
            claim_refs = set(claim["evidence_refs"])
            if not claim_refs <= refs:
                raise VerificationError("claim cites evidence missing from output")
            if claim["verdict"] != "insufficient_evidence" and not claim_refs:
                raise VerificationError("decided claim has no evidence")
        financial = output["financial_resolution"]
        line_total = sum(
            (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
            Decimal("0"),
        )
        recommended = Decimal(str(financial["recommended_refund_brl"]))
        if line_total != recommended:
            raise VerificationError("refund lines do not sum to recommended refund")
        status = output["assessment"]["case_status"]
        actionable = set(output["resolution_actions"]) - {"document_no_action"}
        if status == "no_action" and (recommended > 0 or actionable):
            raise VerificationError("no_action conflicts with refund or resolution actions")
        if status == "action_required" and not output["resolution_actions"]:
            raise VerificationError("action_required has no resolution action")
        if recommended > 0 and not refs:
            raise VerificationError("positive refund has no evidence")
        issue = output["assessment"]["primary_issue"]
        party_types = {
            party["party_type"] for party in output["root_cause_analysis"]["responsible_parties"]
        }
        allowed_parties = _ISSUE_PARTIES[issue]
        if party_types - allowed_parties:
            raise VerificationError("responsible party conflicts with primary issue")
        non_actionable = {"valid_split_payment", "unsupported_claim", "insufficient_evidence"}
        if issue not in non_actionable and not party_types:
            raise VerificationError("actionable issue has no responsible party")
        if issue in {"valid_split_payment", "unsupported_claim"} and status != "no_action":
            raise VerificationError("non-actionable issue must have no_action status")
        if issue == "insufficient_evidence" and status != "needs_investigation":
            raise VerificationError("insufficient evidence must need investigation")
        if recommended > 0 and issue not in _FINANCIAL_ISSUES:
            raise VerificationError("primary issue does not permit a refund")
        refund_actions = {
            "issue_refund",
            "refund_freight",
            "refund_duplicate_charge",
            "retry_refund",
            "retry_failed_refund",
            "reconcile_payment",
            "reconcile_payment_ledger",
            "reverse_duplicate_charge",
        }
        if recommended > 0 and not refund_actions.intersection(output["resolution_actions"]):
            raise VerificationError("positive refund requires issue_refund action")
        refundable = output["payment_analysis"]["refundable_total_brl"]
        if refundable is not None and recommended > Decimal(str(refundable)):
            raise VerificationError("recommended refund exceeds refundable total")
        shipment_verdict = output["shipment_analysis"]["verdict"]
        payment_verdict = output["payment_analysis"]["verdict"]
        if issue == "late_delivery_seller":
            if shipment_verdict != "seller_delay":
                raise VerificationError("seller-delay issue conflicts with shipment verdict")
            late_sellers = set(output["shipment_analysis"]["late_seller_ids"])
            responsible_sellers = {
                party["party_id"]
                for party in output["root_cause_analysis"]["responsible_parties"]
                if party["party_type"] == "seller" and party["party_id"] is not None
            }
            if late_sellers and responsible_sellers != late_sellers:
                raise VerificationError("responsible sellers differ from late sellers")
        if issue == "late_delivery_logistics" and shipment_verdict not in {
            "logistics_delay",
            "lost",
        }:
            raise VerificationError("logistics-delay issue conflicts with shipment verdict")
        payment_issue_verdicts = {
            "payment_mismatch": {"capture_mismatch"},
            "duplicate_charge": {"duplicate_capture"},
            "refund_pending": {"refund_pending"},
            "refund_failed": {"refund_failed"},
            "valid_split_payment": {"reconciled"},
        }
        if issue in payment_issue_verdicts and payment_verdict not in payment_issue_verdicts[issue]:
            raise VerificationError("payment issue conflicts with payment verdict")
        confidence = output["assessment"]["confidence"]
        conflicts = output["data_conflicts"]
        if any(conflict["selected_source"] is None for conflict in conflicts) and confidence > 0.5:
            raise VerificationError("unresolved conflict confidence exceeds 0.5")
        if conflicts and confidence > 0.85:
            raise VerificationError("conflicting evidence confidence exceeds 0.85")
        if issue == "insufficient_evidence" and confidence > 0.5:
            raise VerificationError("insufficient evidence confidence exceeds 0.5")
        incomplete_delivery = (
            issue.startswith("late_delivery")
            and not output["shipment_analysis"]["timeline_complete"]
        )
        if incomplete_delivery and confidence > 0.75:
            raise VerificationError("incomplete shipment timeline confidence exceeds 0.75")
    except (VerificationError, ValueError, KeyError, TypeError) as exc:
        if trace is not None:
            trace.emit(
                case_id=case_id,
                event_type="verification_completed",
                actor="verifier",
                decision_code="FAIL",
            )
        raise VerificationError(str(exc)) from exc
    if trace is not None:
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="PASS",
        )
