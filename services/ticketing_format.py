# services/ticketing_format.py
from __future__ import annotations
from typing import Any

ALLOWED_TRIGGER_TYPES = {"ecomm_new_order", "ecomm_order_changed"}

def _as_str_minor(v: Any) -> str:
    try:
        return str(int(v))
    except Exception:
        return "0"

def _items_from_db(order_row) -> list[dict]:
    items = []
    for it in (order_row.items or []):
        unit = getattr(it, "currency", None) or getattr(order_row, "currency", None) or "THB"
        val = _as_str_minor(getattr(it, "line_total_minor", None) or getattr(it, "unit_price", None) or 0)
        items.append({
            "Count": it.quantity or 1,
            "RowTotal": {  # kept for compatibility if some consumers read it
                "Unit": unit,
                "Value": val,
            },
            "ProductId": it.product_id,
            "ProductName": it.product_name or "",
            "VariantId": getattr(it, "variant_id", None),
            "Price": {
                "Unit": unit,
                "Value": _as_str_minor(getattr(it, "unit_price", None) or 0),
                "String": f"{unit} {_as_str_minor(getattr(it, 'unit_price', None) or 0)}",
            },
        })
    return items

def _accepted_on_from_raw(raw: dict | None) -> str | None:
    """
    Pull the OTA-provided acceptance timestamp.
    Source path: raw['order']['accepted_at'] (ISO8601)
    """
    try:
        return ((raw or {}).get("order") or {}).get("accepted_at") or None
    except Exception:
        return None

def build_ticketing_payload(order_row) -> dict:
    """
    Outbound shape to Soraso expects:
    {
      "TriggerType": "ecomm_order_changed",
      "Payload": {
        "OrderId": "...",
        "Status": "unfulfilled",
        "AcceptedOn": "2025-07-13T06:59:39.561Z",   # NOTE: PascalCase
        "FulfilledOn": null,
        "CustomerPaid": {"Unit": "THB", "Value": "2761", "String": "THB 2761"},
        "CustomerInfo": {"FullName": "...", "Email": "..."},
        "PurchasedItems": [ ... ],
        "PurchasedItemsCount": N,
        "payment": {"processor": "...", "method": "...", "details": {...}},
        "Totals": {"Total": {"Unit": "THB", "Value": "2761", "String": "THB 2761"}}
      }
    }
    """
    src = order_row.raw_ota_payload or {}
    accepted_on = _accepted_on_from_raw(src)

    # If inbound is already wrapped, normalize & enforce expectations
    if isinstance(src, dict) and "TriggerType" in src and "Payload" in src:
        trig = str(src.get("TriggerType") or "").strip()
        trigger_type = trig if trig in ALLOWED_TRIGGER_TYPES else "ecomm_order_changed"

        out = {
            "TriggerType": trigger_type,
            "Payload": dict(src.get("Payload") or {}),
        }
        pl = out["Payload"]

        # Required fields
        pl["OrderId"] = pl.get("OrderId") or getattr(order_row, "order_id", None) or getattr(order_row, "external_order_id", None)
        pl["Status"] = "unfulfilled"

        # AcceptedOn (rename any accepted_at to AcceptedOn)
        pl["AcceptedOn"] = pl.get("AcceptedOn") or pl.pop("accepted_at", None) or accepted_on

        # Customer info
        ci = dict(pl.get("CustomerInfo") or {})
        ci.setdefault("FullName", getattr(order_row, "customer_name", None))
        ci.setdefault("Email", getattr(order_row, "customer_email", None))
        pl["CustomerInfo"] = ci

        # Items
        if not pl.get("PurchasedItems"):
            items = _items_from_db(order_row)
            if items:
                pl["PurchasedItems"] = items
                pl["PurchasedItemsCount"] = len(items)

        # Paid / Totals
        unit = getattr(order_row, "currency", None) or "THB"
        paid_val = _as_str_minor(getattr(order_row, "total_amount", None) or getattr(order_row, "total_amount_minor", None) or 0)
        if not pl.get("CustomerPaid"):
            pl["CustomerPaid"] = {"Unit": unit, "Value": paid_val, "String": f"{unit} {paid_val}"}
        totals = dict(pl.get("Totals") or {})
        if not totals.get("Total"):
            totals["Total"] = {"Unit": unit, "Value": paid_val, "String": f"{unit} {paid_val}"}
        pl["Totals"] = totals

        # Payment
        pay = dict(pl.get("payment") or {})
        pay.setdefault("processor", getattr(order_row, "payment_processor", None))
        pay.setdefault("method", getattr(order_row, "payment_method", None))
        pay.setdefault("details", getattr(order_row, "payment_details", None) or {})
        pl["payment"] = pay

        return out

    # Construct from DB snapshot
    unit = getattr(order_row, "currency", None) or "THB"
    paid_val = _as_str_minor(getattr(order_row, "total_amount", None) or getattr(order_row, "total_amount_minor", None) or 0)
    items = _items_from_db(order_row)

    payload = {
        "OrderId": getattr(order_row, "order_id", None) or getattr(order_row, "external_order_id", None),
        "Status": "unfulfilled",
        "AcceptedOn": accepted_on,     # <-- Renamed here
        "FulfilledOn": None,
        "CustomerPaid": {"Unit": unit, "Value": paid_val, "String": f"{unit} {paid_val}"},
        "CustomerInfo": {"FullName": getattr(order_row, "customer_name", None), "Email": getattr(order_row, "customer_email", None)},
        "PurchasedItems": items,
        "PurchasedItemsCount": len(items),
        "payment": {
            "processor": getattr(order_row, "payment_processor", None),
            "method": getattr(order_row, "payment_method", None),
            "details": getattr(order_row, "payment_details", None) or {},
        },
        "Totals": {"Total": {"Unit": unit, "Value": paid_val, "String": f"{unit} {paid_val}"}},
    }
    return {"TriggerType": "ecomm_order_changed", "Payload": payload}
