"""Pure, evidence-based normalization of public MCP domain records."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import product
from typing import Any


def records(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def ids(values: Any) -> list[str]:
    return sorted({v for v in values if isinstance(v, str) and v})[:20]


def date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def money(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return (
            result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if result.is_finite() and result >= 0
            else None
        )
    except (InvalidOperation, TypeError):
        return None


def conflict(field: str, sources: list[str], selected: str | None) -> dict[str, Any]:
    return {
        "field": field,
        "sources": sources,
        "selected_source": selected,
        "resolution_code": "CASE_TIME_WINDOW" if selected else "UNRESOLVED",
    }


def in_window(value: Any, start: Any, end: Any) -> bool:
    at, lower, upper = date(value), date(start), date(end)
    return at is not None and (lower is None or at >= lower) and (upper is None or at < upper)


def select_order(
    direct: dict[str, Any], history: list[dict[str, Any]], opened_at: Any
) -> tuple[dict[str, Any], str | None, list[dict[str, Any]]]:
    """Choose the latest purchase known at case opening, never a future snapshot."""
    candidates = [
        direct,
        *[row for row in history if row.get("order_id") == direct.get("order_id")],
    ]
    opening = date(opened_at)
    eligible = [row for row in candidates if date(row.get("order_purchase_timestamp")) is not None]
    if opening:
        eligible = [row for row in eligible if date(row["order_purchase_timestamp"]) <= opening]
    if not eligible:
        return {}, None, []
    selected = max(eligible, key=lambda row: date(row["order_purchase_timestamp"]))
    same_time = [
        row
        for row in eligible
        if row["order_purchase_timestamp"] == selected["order_purchase_timestamp"]
    ]
    material = ("order_status", "customer_id", "order_delivered_customer_date")
    if any(any(row.get(key) != selected.get(key) for key in material) for row in same_time):
        return {}, None, [conflict("order_snapshot", ["get_order", "get_customer_history"], None)]
    future = sorted(
        {
            row["order_purchase_timestamp"]
            for row in candidates
            if date(row.get("order_purchase_timestamp"))
            and date(row["order_purchase_timestamp"]) > date(selected["order_purchase_timestamp"])
        },
        key=date,
    )
    conflicts = []
    if selected != direct:
        conflicts.append(
            conflict(
                "order_snapshot", ["get_order", "get_customer_history"], "get_customer_history"
            )
        )
    return selected, future[0] if future else None, conflicts


def analyze_items(rows: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    scoped = [
        row
        for row in rows
        if row.get("order_id") == facts["order_id"]
        and in_window(
            row.get("shipping_limit_date"), facts.get("period_start"), facts.get("period_end")
        )
    ]
    # A source with one untimed row per item can still be used; conflicting
    # duplicates must never be summed as separate purchased items.
    if not scoped and rows and all(not row.get("shipping_limit_date") for row in rows):
        scoped = [row for row in rows if row.get("order_id") == facts["order_id"]]
    unique: dict[str, dict[str, Any]] = {}
    reliable = bool(scoped)
    for row in scoped:
        key = row.get("order_item_id")
        if not isinstance(key, str):
            reliable = False
            continue
        if key in unique and unique[key] != row:
            reliable = False
        unique[key] = row
    amounts = [
        (money(row.get("price")), money(row.get("freight_value"))) for row in unique.values()
    ]
    reliable = reliable and all(p is not None and f is not None for p, f in amounts)
    return {
        "item_ids": ids(unique),
        "seller_ids": ids(row.get("seller_id") for row in unique.values()),
        "order_total_brl": float(sum((p + f for p, f in amounts), Decimal(0)))
        if reliable
        else None,
        "freight_total_brl": float(sum((f for _, f in amounts), Decimal(0))) if reliable else None,
        "order_items_reliable": reliable,
    }


def analyze_shipment(data: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    order = facts["order_record"]
    carrier = date(order.get("order_delivered_carrier_date"))
    delivered = date(order.get("order_delivered_customer_date"))
    estimated = date(order.get("order_estimated_delivery_date"))
    events = [
        event
        for event in records(data.get("events"))
        if in_window(event.get("event_at"), facts.get("period_start"), facts.get("period_end"))
        and event.get("status") == "confirmed"
    ]
    events.sort(key=lambda event: date(event["event_at"]))
    limits = [
        row
        for row in records(data.get("shipping_limits"))
        if in_window(
            row.get("shipping_limit_at"), facts.get("period_start"), facts.get("period_end")
        )
    ]
    late = ids(
        row.get("seller_id")
        for row in limits
        if carrier
        and date(row.get("shipping_limit_at"))
        and carrier > date(row["shipping_limit_at"])
    )
    verdict = "insufficient_evidence"
    if delivered and estimated:
        verdict = (
            ("seller_delay" if late else "logistics_delay") if delivered > estimated else "on_time"
        )
    for event in events:
        kind = event.get("event_type")
        if kind == "delivered_late" and event.get("actor") in {"seller", "logistics_provider"}:
            verdict = "seller_delay" if event["actor"] == "seller" else "logistics_delay"
            delivered = date(event["event_at"])
        elif kind in {"lost", "returned"}:
            verdict = kind
    if verdict != "seller_delay":
        late = []
    conflicts = []
    if data.get("delivered_customer_at") != order.get("order_delivered_customer_date"):
        conflicts.append(
            conflict(
                "shipment.delivered_customer_at",
                ["get_shipment_summary", "get_customer_history"],
                "get_shipment_summary" if events else "get_customer_history",
            )
        )
    return {
        "shipment_verdict": verdict,
        "late_seller_ids": late,
        "timeline_complete": bool(carrier and delivered and estimated),
        "shipment_ids": ids([data.get("shipment_id"), *[e.get("shipment_id") for e in events]]),
        "shipment_conflicts": conflicts,
    }


def analyze_payment(data: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    events = [
        event
        for event in records(data.get("events"))
        if in_window(event.get("event_at"), facts.get("period_start"), facts.get("period_end"))
    ]
    # Deduplicate only by an explicit transaction/event identifier. Equal amounts
    # and timestamps can represent two different legitimate transactions.
    seen: set[str] = set()
    captures = []
    for event in events:
        key = event.get("event_id") or event.get("transaction_id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        if event.get("event_type") in {"captured", "capture"} and event.get("status") in {
            "confirmed",
            "completed",
        }:
            captures.append(event)
    amounts = [money(event.get("amount_brl")) for event in captures]
    paid = (
        float(sum(amounts, Decimal(0))) if amounts and all(a is not None for a in amounts) else None
    )
    return {
        "captured_total_brl": paid,
        "capture_count": len(captures),
        "capture_amounts": [float(a) for a in amounts if a is not None],
        "explicit_mismatch": any(e.get("event_type") == "reconciliation_mismatch" for e in events),
        "explicit_duplicate": any(
            e.get("event_type") in {"duplicate_capture", "duplicate_charge"} for e in events
        ),
        "payment_references": ids(e.get("payment_reference") for e in events),
        "payment_verdict": "insufficient_evidence",
        "payment_rows": records(data.get("payments")),
    }


def reconcile_ledger(facts: dict[str, Any]) -> dict[str, Any]:
    """Resolve alternative ledger rows only with a unique item-total match."""
    total = money(facts.get("order_total_brl"))
    available = Counter(money(amount) for amount in facts.get("capture_amounts", []))
    groups: dict[str, set[tuple[str, Decimal]]] = {}
    for row in facts.get("payment_rows", []):
        sequential, amount = row.get("payment_sequential"), money(row.get("payment_value"))
        if sequential is not None and amount is not None and amount in available:
            groups.setdefault(str(sequential), set()).add((str(row.get("payment_type")), amount))
    if total is None or not any(len(options) > 1 for options in groups.values()):
        return {}
    combinations = 1
    for options in groups.values():
        combinations *= len(options)
    if combinations > 256:
        return {"payment_ambiguous": True}
    matches = []
    for combination in product(*groups.values()):
        amounts = [amount for _, amount in combination]
        if sum(amounts, Decimal(0)) == total and not (Counter(amounts) - available):
            matches.append(amounts)
    resolved = len(matches) == 1
    result: dict[str, Any] = {
        "payment_ambiguous": not resolved,
        "payment_conflicts": [
            conflict(
                "payment.captured_total_brl",
                ["get_payment_timeline", "get_order_items"],
                "get_payment_timeline" if resolved else None,
            )
        ],
    }
    if resolved:
        result.update(captured_total_brl=float(total), capture_count=len(matches[0]))
        result["payment_conflicts"][0]["resolution_code"] = "UNIQUE_LEDGER_RECONCILIATION"
    return result


def analyze_refund(data: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    events = [
        event
        for event in records(data.get("events"))
        if in_window(event.get("event_at"), facts.get("period_start"), facts.get("period_end"))
    ]
    events.sort(key=lambda event: date(event["event_at"]))
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event.get("refund_id") or event.get("refund_reference") or "unidentified-request"
        latest[key] = event
    successful = [
        money(e.get("amount_brl"))
        for e in latest.values()
        if e.get("status") in {"confirmed", "completed", "succeeded"}
    ]
    refunded = (
        float(sum(successful, Decimal(0))) if all(a is not None for a in successful) else None
    )
    statuses = {e.get("status") for e in latest.values()}
    verdict = (
        "refund_failed"
        if "failed" in statuses
        else "refund_pending"
        if "pending" in statuses
        else "refunded"
        if successful
        else None
    )
    return {"refunded_total_brl": refunded, "refund_verdict": verdict, "refund_checked": True}
