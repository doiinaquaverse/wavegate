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
        items.append({
            "Count": it.quantity or 1,
            "RowTotal": {
                "Unit": order_row.currency or "THB",
                "Value": _as_str_minor(it.line_total_minor or it.unit_price_minor or 0),
            },
            "ProductId": it.product_id,
            "ProductName": it.product_name or "",
        })
    return items

def build_ticketing_payload(order_row) -> dict:
    """
    Outbound shape:
    {
      "TriggerType": "ecomm_new_order" | "ecomm_order_changed",
      "Payload": {
        "OrderId": "...",
        "Status": "unfulfilled",
        "CustomerInfo": {"FullName": "...", "Email": "..."},
        "Totals": {"Total": {"Unit": "THB", "Value": "2761"}},
        "PurchasedItems": [ ... ],
        "PurchasedItemsCount": N
      }
    }
    - If inbound raw payload already has TriggerType/Payload, we keep TriggerType
      only if it's allowed; else default to "ecomm_new_order".
    - We always force Payload.Status = "unfulfilled" (tickets not generated yet).
    """
    src = order_row.raw_ota_payload or {}

    # If inbound is already wrapped, normalize & enforce expectations
    if isinstance(src, dict) and "TriggerType" in src and "Payload" in src:
        trig = str(src.get("TriggerType") or "").strip()
        trigger_type = trig if trig in ALLOWED_TRIGGER_TYPES else "ecomm_new_order"

        out = {
            "TriggerType": trigger_type,
            "Payload": dict(src.get("Payload") or {}),
        }
        pl = out["Payload"]

        # Required fields
        pl["OrderId"] = pl.get("OrderId") or order_row.external_order_id
        pl["Status"] = "unfulfilled"

        # Customer info (best effort fill)
        ci = dict(pl.get("CustomerInfo") or {})
        ci.setdefault("FullName", order_row.customer_name)
        ci.setdefault("Email", order_row.customer_email)
        pl["CustomerInfo"] = ci

        # Items (only if missing)
        if not pl.get("PurchasedItems"):
            items = _items_from_db(order_row)
            if items:
                pl["PurchasedItems"] = items
                pl["PurchasedItemsCount"] = len(items)

        # Totals (best effort)
        totals = dict(pl.get("Totals") or {})
        if not totals.get("Total"):
            totals["Total"] = {
                "Unit": order_row.currency or "THB",
                "Value": _as_str_minor(order_row.total_amount_minor or 0),
            }
        pl["Totals"] = totals

        return out

    # Construct minimal wrapped payload from DB fields
    items = _items_from_db(order_row)
    payload = {
        "OrderId": order_row.external_order_id,
        "Status": "unfulfilled",
        "CustomerInfo": {
            "FullName": order_row.customer_name,
            "Email": order_row.customer_email,
        },
        "Totals": {
            "Total": {
                "Unit": order_row.currency or "THB",
                "Value": _as_str_minor(order_row.total_amount_minor or 0),
            }
        },
        "PurchasedItems": items,
        "PurchasedItemsCount": len(items),
    }
    return {"TriggerType": "ecomm_new_order", "Payload": payload}
