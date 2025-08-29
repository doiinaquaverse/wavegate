# services/ticketing_format.py
from __future__ import annotations
from typing import Any

def _as_str_minor(v: Any) -> str:
    try:
        return str(int(v))
    except Exception:
        return "0"

def build_soraso_payload(order_row) -> dict:
    items = []
    for it in (order_row.items or []):
        unit = it.currency or order_row.currency or "THB"
        val = _as_str_minor(it.unit_price or 0)
        items.append({
            "Count": it.quantity or 1,
            "ProductId": it.product_id,
            "ProductName": it.product_name or "",
            "VariantId": it.variant_id,
            "Price": {
                "Unit": unit,
                "Value": val,
                "String": f"{unit} {val}",
            },
        })

    # accepted_at from raw OTA (if present)
    accepted_at = None
    try:
        accepted_at = (order_row.raw_ota_payload.get("order") or {}).get("accepted_at")
    except Exception:
        accepted_at = None

    paid_val = _as_str_minor(order_row.total_amount or 0)
    unit = order_row.currency or "THB"

    payload = {
        "OrderId": order_row.order_id,
        "Status": "unfulfilled",
        "accepted_at": accepted_at,
        "FulfilledOn": None,

        # Your spec
        "CustomerPaid": {"Unit": unit, "Value": paid_val, "String": f"{unit} {paid_val}"},
        "CustomerInfo": {"FullName": order_row.customer_name, "Email": order_row.customer_email},
        "PurchasedItems": items,
        "PurchasedItemsCount": len(items),
        "payment": {
            "processor": order_row.payment_processor,
            "method": order_row.payment_method,
            "details": order_row.payment_details or {},
        },

        # Extra compatibility (some APIs expect Totals.Total in addition to CustomerPaid)
        "Totals": {"Total": {"Unit": unit, "Value": paid_val, "String": f"{unit} {paid_val}"}},
    }
    return {"TriggerType": "ecomm_order_changed", "Payload": payload}
