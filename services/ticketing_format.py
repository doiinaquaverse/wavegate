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

def _fmt_amount_string(unit: str, minor_value: Any) -> str:
    # Generic "amount UNIT" format. Add currency-symbol mapping if Soraso requires symbols.
    cents = _to_int_minor(minor_value)
    major = cents / 100.0
    return f"{major:,.2f} {unit or 'THB'}"

def _ensure_amount_object(obj: dict | None, fallback_unit: str, fallback_value_minor: Any) -> dict:
    d = dict(obj or {})
    unit = (d.get("unit") or fallback_unit or "THB")
    val_minor = d.get("value")
    if val_minor is None:
        val_minor = fallback_value_minor
    val_minor_str = _as_str_minor(val_minor)
    d["unit"] = unit
    d["value"] = val_minor_str
    d["string"] = _fmt_amount_string(unit, val_minor_str)
    return d

def _items_from_db(order_row) -> list[dict]:
    items = []
    for it in (order_row.items or []):
        unit = getattr(it, "currency", None) or getattr(order_row, "currency", None) or "THB"
        unit_price_minor = getattr(it, "unit_price", None) or 0
        qty = it.quantity or 1
        row_total_minor = unit_price_minor * qty

        items.append({
            "count": qty,
            "rowTotal": _ensure_amount_object({"unit": unit, "value": _as_str_minor(row_total_minor)}, unit, row_total_minor),
            "productId": it.product_id,
            "productName": it.product_name or "",
            "productSlug": getattr(it, "product_slug", None),
            "variantId": getattr(it, "variant_id", None),
            "variantName": getattr(it, "variant_name", None),
            "variantSlug": getattr(it, "variant_slug", None),
            "variantSKU": getattr(it, "variant_sku", None),
            "variantImage": {"url": getattr(it, "variant_image_url", None), "file": {}},
            "variantPrice": _ensure_amount_object({"unit": unit, "value": _as_str_minor(unit_price_minor)}, unit, unit_price_minor),
            "weight": 0, "width": 0, "height": 0, "length": 0,
        })
    return items

def _accepted_on_from_raw(raw: dict | None) -> str | None:
    try:
        return ((raw or {}).get("order") or {}).get("accepted_at") or None
    except Exception:
        return None

def _addresses_from_raw(raw: dict | None) -> tuple[list[dict], dict, dict]:
    # Stub until addresses are captured upstream. Extend when available.
    all_addresses: list[dict] = []
    shipping = {}
    billing = {}
    return all_addresses, shipping, billing

def _totals_from_db(order_row) -> dict:
    unit = getattr(order_row, "currency", None) or "THB"
    paid_minor = getattr(order_row, "total_amount", None) or 0
    subtotal = _ensure_amount_object({"unit": unit, "value": _as_str_minor(paid_minor)}, unit, paid_minor)
    total = _ensure_amount_object({"unit": unit, "value": _as_str_minor(paid_minor)}, unit, paid_minor)
    return {
        "subtotal": subtotal,
        "extras": [],
        "total": total,
    }

def _stripe_sections(order_row) -> tuple[dict, dict]:
    pd = order_row.payment_details or {}
    stripe_details = {
        "subscriptionId": pd.get("subscription_id"),
        "paymentMethod": pd.get("payment_method") or pd.get("method") or pd.get("payment_method_id"),
        "paymentIntentId": pd.get("payment_intent_id"),
        "customerId": pd.get("customer_id"),
        "chargeId": pd.get("charge_id"),
        "disputeId": pd.get("dispute_id"),
        "refundId": pd.get("refund_id"),
        "refundReason": pd.get("refund_reason"),
    }
    stripe_card = {
        "last4": pd.get("card_last4"),
        "brand": pd.get("card_brand"),
        "ownerName": pd.get("card_owner"),
        "expires": {
            "year": pd.get("card_exp_year"),
            "month": pd.get("card_exp_month"),
        }
    }
    return stripe_details, stripe_card

def build_soraso_payload(order_row, *, status: str = "unfulfilled", wrap: bool = True) -> dict:
    """
    Build Soraso/Webflow-like payload using lower camelCase keys.
    - If wrap=True: {"triggerType": "ecomm_order_changed", "payload": {...}}
    - If wrap=False: return only the inner payload (used for fulfill echo)
    """
    src = order_row.raw_ota_payload or {}
    accepted_on = _accepted_on_from_raw(src)
    unit = getattr(order_row, "currency", None) or "THB"
    paid_minor = getattr(order_row, "total_amount", None) or 0
    paid_minor_str = _as_str_minor(paid_minor)
    all_addresses, shipping_addr, billing_addr = _addresses_from_raw(src)
    stripe_details, stripe_card = _stripe_sections(order_row)

    payload = {
        "orderId": getattr(order_row, "order_id", None),
        "status": status,  # "unfulfilled" for initial push; "fulfilled" for fulfill echo
        "comment": "",
        "orderComment": "",
        "acceptedOn": accepted_on,
        "fulfilledOn": None,
        "refundedOn": None,
        "disputedOn": None,
        "disputeUpdatedOn": None,
        "disputeLastStatus": None,
        "customerPaid": _ensure_amount_object({"unit": unit, "value": paid_minor_str}, unit, paid_minor_str),
        "netAmount": _ensure_amount_object({"unit": unit, "value": 0}, unit, 0),
        "applicationFee": _ensure_amount_object({"unit": unit, "value": 0}, unit, 0),
        "allAddresses": all_addresses,
        "shippingAddress": shipping_addr,
        "billingAddress": billing_addr,
        "shippingProvider": None,
        "shippingTracking": None,
        "shippingTrackingURL": None,
        "customerInfo": {
            "fullName": getattr(order_row, "customer_name", None),
            "email": getattr(order_row, "customer_email", None),
        },
        "purchasedItems": _items_from_db(order_row),
        "purchasedItemsCount": len(order_row.items or []),
        "stripeDetails": stripe_details,
        "stripeCard": stripe_card,
        "paypalDetails": {},
        "customData": [{}],
        "metadata": {
            "isBuyNow": False,
            "hasDownloads": False,
            "paymentProcessor": getattr(order_row, "payment_processor", None),
        },
        "isCustomerDeleted": False,
        "isShippingRequired": False,
        "totals": _totals_from_db(order_row),
        "downloadFiles": [],
    }

    if wrap:
        return {"triggerType": "ecomm_order_changed", "payload": payload}
    return payload
