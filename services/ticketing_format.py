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
    cents = _to_int_minor(minor_value)
    major = cents / 100.0
    # Soraso examples show a formatted string; plain space is fine unless they require NBSP.
    return f"{major:,.2f} {unit or 'THB'}"

def _ensure_amount_string(obj: dict | None, fallback_unit: str, fallback_value_minor: Any) -> dict:
    d = dict(obj or {})
    unit = (d.get("Unit") or fallback_unit or "THB")
    val_minor = d.get("Value")
    if val_minor is None:
        val_minor = fallback_value_minor
    val_minor_str = _as_str_minor(val_minor)
    d["Unit"] = unit
    d["Value"] = val_minor_str
    d["String"] = _fmt_major_with_unit(unit, val_minor_str)
    return d

def _items_from_db(order_row) -> list[dict]:
    items = []
    for it in (order_row.items or []):
        unit = getattr(it, "currency", None) or getattr(order_row, "currency", None) or "THB"
        unit_price_minor = getattr(it, "unit_price", None) or 0
        row_total_minor = unit_price_minor * (it.quantity or 1)

        items.append({
            "Count": it.quantity or 1,
            "RowTotal": _ensure_amount_string({"Unit": unit, "Value": _as_str_minor(row_total_minor)}, unit, row_total_minor),
            "ProductId": it.product_id,
            "ProductName": it.product_name or "",
            "ProductSlug": it.product_slug,
            "VariantId": getattr(it, "variant_id", None),
            "VariantName": getattr(it, "variant_name", None),
            "VariantSlug": getattr(it, "variant_slug", None),
            "VariantSKU": getattr(it, "variant_sku", None),
            "VariantImage": {"Url": getattr(it, "variant_image_url", None)},
            "VariantPrice": _ensure_amount_string({"Unit": unit, "Value": _as_str_minor(unit_price_minor)}, unit, unit_price_minor),
            "Weight": 0, "Width": 0, "Height": 0, "Length": 0,
        })
    return items

def _accepted_on_from_raw(raw: dict | None) -> str | None:
    try:
        return ((raw or {}).get("order") or {}).get("accepted_at") or None
    except Exception:
        return None

def build_ticketing_payload(order_row) -> dict:
    """
    Build Soraso/“Webflow-like” payload exactly as they shared.
    """
    src = order_row.raw_ota_payload or {}
    accepted_on = _accepted_on_from_raw(src)
    unit = getattr(order_row, "currency", None) or "THB"
    paid_minor = getattr(order_row, "total_amount", None) or 0
    paid_minor_str = _as_str_minor(paid_minor)

    # Base skeleton
    payload = {
        "OrderId": getattr(order_row, "order_id", None),
        "Status": "unfulfilled",
        "Comment": "",
        "OrderComment": "",
        "AcceptedOn": accepted_on,
        "FulfilledOn": None,
        "RefundedOn": None,
        "DisputedOn": None,
        "DisputeUpdatedOn": None,
        "DisputeLastStatus": None,
        "CustomerPaid": _ensure_amount_string({"Unit": unit, "Value": paid_minor_str}, unit, paid_minor_str),
        "NetAmount": _ensure_amount_string({"Unit": unit, "Value": 0}, unit, 0),
        "ApplicationFee": _ensure_amount_string({"Unit": unit, "Value": 0}, unit, 0),
        "AllAddresses": [],
        "ShippingAddress": {},
        "BillingAddress": {},
        "ShippingProvider": None,
        "ShippingTracking": None,
        "ShippingTrackingURL": None,
        "CustomerInfo": {
            "FullName": getattr(order_row, "customer_name", None),
            "Email": getattr(order_row, "customer_email", None),
        },
        "PurchasedItems": _items_from_db(order_row),
        "PurchasedItemsCount": len(order_row.items or []),
        "StripeDetails": {
            "PaymentMethod": (order_row.payment_details or {}).get("payment_method"),
            "PaymentIntentId": (order_row.payment_details or {}).get("payment_intent_id"),
            "CustomerId": (order_row.payment_details or {}).get("customer_id"),
            "ChargeId": (order_row.payment_details or {}).get("charge_id"),
            "RefundId": (order_row.payment_details or {}).get("refund_id"),
            "RefundReason": (order_row.payment_details or {}).get("refund_reason"),
        },
        "StripeCard": {
            "Last4": (order_row.payment_details or {}).get("card_last4"),
            "Brand": (order_row.payment_details or {}).get("card_brand"),
            "OwnerName": (order_row.payment_details or {}).get("card_owner"),
            "Expires": {
                "Year": (order_row.payment_details or {}).get("card_exp_year"),
                "Month": (order_row.payment_details or {}).get("card_exp_month"),
            }
        },
        "CustomData": [],
        "Metadata": {"IsBuyNow": False},
        "IsCustomerDeleted": False,
        "IsShippingRequired": False,
        "HasDownloads": False,
        "PaymentProcessor": getattr(order_row, "payment_processor", None),
        "Totals": {
            "Subtotal": _ensure_amount_string({"Unit": unit, "Value": paid_minor_str}, unit, paid_minor_str),
            "Extras": [],
            "Total": _ensure_amount_string({"Unit": unit, "Value": paid_minor_str}, unit, paid_minor_str),
        },
        "DownloadFiles": [],
    }

    # Wrap with TriggerType
    out = {
        "TriggerType": "ecomm_order_changed",
        "Payload": payload,
    }
    return out
