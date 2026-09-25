"""Deterministic dispute policy and confidence calibration.

The engine consumes normalized facts produced by specialists.  It never invents an
amount: a refund is positive only when a specialist supplied an authoritative
``refundable_total_brl`` (or an equivalent calculable excess capture).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_MONEY = Decimal("0.01")


def _money(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    amount = Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    if not amount.is_finite() or amount < 0:
        raise ValueError("money values must be non-negative")
    return amount


def _party(party_type: str, party_id: str | None = None) -> dict[str, Any]:
    return {"party_type": party_type, "party_id": party_id}


def _unique_strings(values: Sequence[Any]) -> list[str]:
    return sorted({value for value in values if isinstance(value, str) and value})


def calibrate_confidence(
    *,
    entity_confidence: float,
    evidence_coverage: float,
    has_unresolved_conflict: bool = False,
    has_resolved_conflict: bool = False,
    analysis_complete: bool = True,
    insufficient_evidence: bool = False,
) -> float:
    """Return a conservative confidence score derived from the weakest link."""
    values = (entity_confidence, evidence_coverage)
    if any(isinstance(value, bool) or not 0 <= value <= 1 for value in values):
        raise ValueError("confidence inputs must be numbers in [0, 1]")
    confidence = min(values)
    if insufficient_evidence or has_unresolved_conflict:
        confidence = min(confidence, 0.5)
    elif has_resolved_conflict:
        confidence = min(confidence, 0.85)
    if not analysis_complete:
        confidence = min(confidence, 0.75)
    return round(confidence, 2)


def decide_policy(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Map normalized specialist facts to the policy-controlled output sections."""
    order_status = str(facts.get("order_status") or "").lower()
    shipment = str(facts.get("shipment_verdict") or "insufficient_evidence")
    payment = str(facts.get("payment_verdict") or "insufficient_evidence")
    paid = _money(facts.get("captured_total_brl"))
    refunded = _money(facts.get("refunded_total_brl"))
    order_total = _money(facts.get("order_total_brl"))
    explicit_refundable = facts.get("refundable_total_brl")
    seller_ids = _unique_strings(facts.get("late_seller_ids") or facts.get("seller_ids") or [])

    if order_status == "canceled" and paid > refunded:
        issue = "canceled_order_paid"
    elif order_status in {"unavailable", "unfulfillable"} and paid > refunded:
        issue = "unavailable_order_paid"
    elif payment == "duplicate_capture":
        issue = "duplicate_charge"
    elif payment == "refund_failed":
        issue = "refund_failed"
    elif payment == "refund_pending":
        issue = "refund_pending"
    elif payment == "capture_mismatch":
        issue = "payment_mismatch"
    elif shipment == "seller_delay":
        issue = "late_delivery_seller"
    elif shipment in {"logistics_delay", "lost"}:
        issue = "late_delivery_logistics"
    elif payment == "reconciled" and bool(facts.get("is_valid_split_payment")):
        issue = "valid_split_payment"
    elif bool(facts.get("claim_disproved")):
        issue = "unsupported_claim"
    else:
        issue = "insufficient_evidence"

    if explicit_refundable is not None:
        refundable = _money(explicit_refundable)
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        refundable = max(paid - refunded, Decimal("0"))
    elif issue == "duplicate_charge" and order_total > 0:
        refundable = max(paid - order_total - refunded, Decimal("0"))
    else:
        refundable = Decimal("0")

    parties: list[dict[str, Any]]
    if issue == "late_delivery_seller":
        parties = [_party("seller", seller_id) for seller_id in seller_ids]
        if not parties:
            parties = [_party("seller")]
    elif issue == "late_delivery_logistics":
        parties = [_party("logistics_provider", facts.get("logistics_provider_id"))]
    elif issue in {"payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}:
        parties = [_party("payment_provider", facts.get("payment_provider_id"))]
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        parties = (
            [_party("seller", seller_id) for seller_id in seller_ids]
            if seller_ids
            else [_party("platform")]
        )
    else:
        parties = []

    action_by_issue = {
        "canceled_order_paid": "issue_refund",
        "unavailable_order_paid": "issue_refund",
        "late_delivery_seller": "review_seller_fulfillment",
        "late_delivery_logistics": "open_logistics_investigation",
        "payment_mismatch": "reconcile_payment_ledger",
        "duplicate_charge": "reverse_duplicate_charge",
        "refund_pending": "complete_pending_refund",
        "refund_failed": "retry_failed_refund",
    }
    action = action_by_issue.get(issue)
    actions = [action] if action else []
    if refundable > 0 and "issue_refund" not in actions:
        actions.append("issue_refund")

    case_status = (
        "needs_investigation"
        if issue == "insufficient_evidence"
        else "no_action"
        if issue in {"valid_split_payment", "unsupported_claim"}
        else "action_required"
    )
    # A server policy is authoritative for action/eligibility. Monetary policy
    # amounts are bounded by the scoped captured balance, never by another case.
    rules = facts.get("policy_rules")
    if isinstance(rules, Mapping):
        rule = rules.get(issue)
        if isinstance(rule, Mapping):
            status = rule.get("case_status")
            if status in {"action_required", "needs_investigation", "no_action"}:
                case_status = status
            action = rule.get("recommended_action")
            actions = [action] if isinstance(action, str) and action else []
            refundable = min(_money(rule.get("refund_brl")), max(paid - refunded, Decimal("0")))
            parties = []
            for party in rule.get("responsible_parties", []):
                party_type = party.get("party_type")
                if party_type == "seller":
                    parties.extend(_party("seller", seller) for seller in seller_ids)
                elif party_type in {
                    "platform",
                    "logistics_provider",
                    "payment_provider",
                    "customer",
                    "unknown",
                }:
                    # Policy IDs are not proof of case ownership.
                    parties.append(_party(party_type))
            if case_status == "no_action":
                refundable = Decimal("0")
        elif issue != "insufficient_evidence":
            issue, case_status = "insufficient_evidence", "needs_investigation"
            refundable, parties, actions = Decimal("0"), [], []
    if facts.get("has_unresolved_conflict"):
        issue, case_status = "insufficient_evidence", "needs_investigation"
        refundable, parties, actions = Decimal("0"), [], []
    has_unresolved_conflict = bool(facts.get("has_unresolved_conflict"))
    has_resolved_conflict = bool(facts.get("has_resolved_conflict"))
    confidence = calibrate_confidence(
        entity_confidence=float(facts.get("entity_confidence", 0.5)),
        evidence_coverage=float(facts.get("evidence_coverage", 0.5)),
        has_unresolved_conflict=has_unresolved_conflict,
        has_resolved_conflict=has_resolved_conflict,
        analysis_complete=bool(facts.get("analysis_complete", False)),
        insufficient_evidence=issue == "insufficient_evidence",
    )
    refund_number = float(refundable)
    return {
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": _unique_strings(facts.get("secondary_issues") or []),
            "case_status": case_status,
            "confidence": confidence,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_number,
            "refund_lines": (
                [
                    {
                        "reason_code": issue.upper(),
                        "amount_brl": refund_number,
                        "entity_id": facts.get("order_id"),
                    }
                ]
                if refundable > 0
                else []
            ),
        },
        "resolution_actions": actions,
    }
