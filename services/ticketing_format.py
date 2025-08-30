# services/ticketing_format.py
from __future__ import annotations
from typing import Any

ALLOWED_TRIGGER_TYPES = {"ecomm_new_order", "ecomm_order_changed"}

def _as_str_minor(v: Any) -> str:
    try:
        return str(int(v))
    except Exception:
        return "0"

def _to_int_minor(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0

def _fmt_major_with_unit(unit: str, minor_value: Any) -> str:
    """Render minor units (e.g., 2761) as '27.61 THB' with thousands separators."""
    cents = _to_int_minor(minor_value)
    major = cents / 100.0
    return f"{major:,.2f} {unit or 'THB'}"

def _ensure_amount_string(obj: dict | None, fallback_unit: str, fallback_value_minor: Any) -> dict:
    """
    Ensure dict has Unit, Value (minor), and a String formatted as '<major> <UNIT>'.
    Returns a new dict.
    """
    d = dict(obj or {})
    unit = (d.get("Unit") or fallback_unit or "THB")
    val_minor = d.get("Value")
    if val_minor is None:
        val_minor = fallback_value_minor
    d["Unit"] = unit
    d["Value"] = _as_str_minor(val_minor)
    d["String"] = _fmt_major_with_unit(unit, d["Value"])
    return d

def _items_from_db(order_row) -> list[dict]:
    items = []
    for it in (order_row.items or []):
        unit = getattr(it, "currency", None) or getattr(order_row, "currency", None) or "THB"
        unit_price_minor = getattr(it, "unit_price", None) or 0
        row_total_minor = getattr(it, "line_total_minor", None) or unit_price_minor or 0

        items.append({
            "Count": it.quantity or 1,
            "RowTotal": _ensure_amount_string(
                {"Unit": unit, "Value": _as_str_minor(row_total_minor)},
                fallback_unit=unit,
                fallback_value_minor=row_total_minor,
            ),
            "ProductId": it.product_id,
            "ProductName": it.product_name or "",
            "VariantId": getattr(it, "variant_id", None),
            "Price": _ensure_amount_string(
                {"Unit": unit, "Value": _as_str_minor(unit_price_minor)},
                fallback_unit=unit,
                fallback_value_minor=unit_price_minor,
            ),
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
    Outbound shape to Soraso:
    {
      "TriggerType": "ecomm_order_changed",
      "Payload": {
        "OrderId": "...",
        "Status": "unfulfilled",
        "AcceptedOn": "2025-07-13T06:59:39.561Z",
        "FulfilledOn": null,
        "CustomerPaid": {"Unit": "THB", "Value": "2761", "String": "27.61 THB"},
        "CustomerInfo": {"FullName": "...", "Email": "..."},
        "PurchasedItems": [ ... Price/RowTotal with String ... ],
        "PurchasedItemsCount": N,
        "payment": {"processor": "...", "method": "...", "details": {...}},
        "Totals": {"Total": {"Unit": "THB", "Value": "2761", "String": "27.61 THB"}}
      }
    }
    """
    src = order_row.raw_ota_payload or {}
    accepted_on = _accepted_on_from_raw(src)
    unit = getattr(order_row, "currency", None) or "THB"
    paid_minor = (
        getattr(order_row, "total_amount", None)
        or getattr(order_row, "total_amount_minor", None)
        or 0
    )
    paid_minor_str = _as_str_minor(paid_minor)

    # --- If inbound was already wrapped, normalize & enforce expectations ---
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

        # Items: inject if missing; also ensure String formatting where present
        if not pl.get("PurchasedItems"):
            items = _items_from_db(order_row)
            if items:
                pl["PurchasedItems"] = items
                pl["PurchasedItemsCount"] = len(items)
        else:
            # Normalize Price/RowTotal.String for existing items
            norm_items = []
            for it in pl.get("PurchasedItems") or []:
                it = dict(it or {})
                price = _ensure_amount_string(
                    it.get("VariantPrice") or it.get("Price"),
                    fallback_unit=unit,
                    fallback_value_minor=it.get("RowTotal", {}).get("Value") or paid_minor_str,
                )
                row_total = _ensure_amount_string(
                    it.get("RowTotal"),
                    fallback_unit=price.get("Unit", unit),
                    fallback_value_minor=it.get("RowTotal", {}).get("Value") or price.get("Value") or paid_minor_str,
                )
                it["Price"] = price
                it["RowTotal"] = row_total
                norm_items.append(it)
            pl["PurchasedItems"] = norm_items
            pl.setdefault("PurchasedItemsCount", len(norm_items))

        # CustomerPaid
        if pl.get("CustomerPaid"):
            pl["CustomerPaid"] = _ensure_amount_string(pl["CustomerPaid"], fallback_unit=unit, fallback_value_minor=paid_minor_str)
        else:
            pl["CustomerPaid"] = _ensure_amount_string({"Unit": unit, "Value": paid_minor_str}, fallback_unit=unit, fallback_value_minor=paid_minor_str)

        # Totals.Total
        totals = dict(pl.get("Totals") or {})
        totals["Total"] = _ensure_amount_string(totals.get("Total"), fallback_unit=unit, fallback_value_minor=paid_minor_str)
        pl["Totals"] = totals

        # Payment (fill missing)
        pay = dict(pl.get("payment") or {})
        pay.setdefault("processor", getattr(order_row, "payment_processor", None))
        pay.setdefault("method", getattr(order_row, "payment_method", None))
        pay.setdefault("details", getattr(order_row, "payment_details", None) or {})
        pl["payment"] = pay

        return out

    # --- Construct from DB snapshot ---
    items = _items_from_db(order_row)

    payload = {
        "OrderId": getattr(order_row, "order_id", None) or getattr(order_row, "external_order_id", None),
        "Status": "unfulfilled",
        "AcceptedOn": accepted_on,
        "FulfilledOn": None,
        "CustomerPaid": _ensure_amount_string({"Unit": unit, "Value": paid_minor_str}, fallback_unit=unit, fallback_value_minor=paid_minor_str),
        "CustomerInfo": {
            "FullName": getattr(order_row, "customer_name", None),
            "Email": getattr(order_row, "customer_email", None),
        },
        "PurchasedItems": items,
        "PurchasedItemsCount": len(items),
        "payment": {
            "processor": getattr(order_row, "payment_processor", None),
            "method": getattr(order_row, "payment_method", None),
            "details": getattr(order_row, "payment_details", None) or {},
        },
        "Totals": {
            "Total": _ensure_amount_string({"Unit": unit, "Value": paid_minor_str}, fallback_unit=unit, fallback_value_minor=paid_minor_str)
        },
    }
    return {"TriggerType": "ecomm_order_changed", "Payload": payload}
