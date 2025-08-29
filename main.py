# main.py
from __future__ import annotations

import os
import time
import socket
import ipaddress
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from typing import Iterable, Set, Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Depends, BackgroundTasks, Request, Body
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from db import get_db
from models import (
    Order, OrderItem, Partner, PartnerStatus,
    IdempotencyKey, PipelineStatus, FulfillmentStatus,
    PartnerRegistration, AuthEvent, AuthFailReason,
)
from workers.ticketing import process_ticketing

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "WaveGate")
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes", "on")
app = FastAPI(title=APP_NAME, debug=DEBUG)
bangkok_timezone = timezone(timedelta(hours=7))

# Trust only these proxy CIDRs when reading client IP headers (e.g., Cloudflare egress)
TRUSTED_PROXY_CIDRS = os.getenv("TRUSTED_PROXY_CIDRS", "")


# ---------------- Health ----------------
@app.get("/health")
async def health():
    return {
        "status": f"{APP_NAME} is healthy",
        "timestamp": datetime.now(bangkok_timezone).strftime("%Y-%m-%d %H:%M:%S"),
    }

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs", status_code=307)


# ---------------- Helpers: csv/ip/ssrf ----------------
def _csv_set(raw: str | None) -> Set[str]:
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}

def _ip_in_cidrs(ip: str, cidrs: Iterable[str]) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if ip_obj in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False

def _trusted_proxy_peer(peer_ip: str) -> bool:
    return _ip_in_cidrs(peer_ip, _csv_set(TRUSTED_PROXY_CIDRS))

def _client_ip(request: Request) -> str:
    """Trust CF/LB headers only when the TCP peer is a trusted proxy."""
    peer = request.client.host if request.client else ""
    if _trusted_proxy_peer(peer):
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            try:
                ipaddress.ip_address(cf); return cf
            except ValueError:
                pass
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            try:
                ipaddress.ip_address(first); return first
            except ValueError:
                pass
    return peer or ""

def enforce_ip_allowlist(request: Request, allowlist_source_cidrs: str | None) -> None:
    """Used **only** for OTA ingress (/orders POST)."""
    cidrs = _csv_set(allowlist_source_cidrs)
    if not cidrs:
        return
    ip = _client_ip(request)
    if not _ip_in_cidrs(ip, cidrs):
        raise HTTPException(status_code=403, detail="Source IP not allowed")

def _is_private_or_loopback_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, 0, 0, 0, socket.AI_ADDRCONFIG)
    except socket.gaierror:
        return True
    for _, _, _, _, sockaddr in infos:
        ip = sockaddr[0].split("%")[0]
        ip_obj = ipaddress.ip_address(ip)
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
            or ip_obj.is_reserved or ip_obj.is_multicast):
            return True
    return False

def _host_matches_allowlist(host: str, allow: Set[str]) -> bool:
    host = host.lower()
    for dom in allow:
        if host == dom or host.endswith("." + dom):
            return True
    return False

def validate_callback_url(url: str, allowlist_domains: str | None) -> str:
    if not url:
        raise HTTPException(status_code=400, detail="callback_url required")
    p = urlparse(url)
    if p.scheme.lower() != "https":
        raise HTTPException(status_code=400, detail="callback_url must use https")
    if p.port not in (None, 443):
        raise HTTPException(status_code=400, detail="callback_url must use port 443")
    if not p.hostname:
        raise HTTPException(status_code=400, detail="callback_url host missing")

    host = p.hostname.lower()
    if host == "localhost" or _is_private_or_loopback_host(host):
        raise HTTPException(status_code=403, detail="callback_url host not allowed")

    allow = _csv_set(allowlist_domains)
    if allow and not _host_matches_allowlist(host, allow):
        raise HTTPException(status_code=403, detail="callback_url domain not allowed")

    return p.geturl()


# ---------------- Auth ----------------
async def verify_partner(db: Session, request: Request, partner_id: str, partner_token: str) -> Partner:
    """Partner credential check only (no IP allowlist here)."""
    def _log(reason: AuthFailReason):
        db.add(AuthEvent(
            partner_id=partner_id,
            ip=_client_ip(request),
            reason=reason,
            user_agent=request.headers.get("user-agent"),
        ))
        db.commit()

    p: Partner | None = db.query(Partner).filter(Partner.partner_id == partner_id).first()
    if not p:
        _log(AuthFailReason.no_partner)
        raise HTTPException(status_code=401, detail="Unauthorized")
    if p.status != PartnerStatus.active:
        _log(AuthFailReason.inactive)
        raise HTTPException(status_code=401, detail="Unauthorized")
    if p.partner_token != partner_token:
        _log(AuthFailReason.bad_token)
        raise HTTPException(status_code=401, detail="Unauthorized")

    p.last_seen_at = func.now()
    db.add(p); db.commit()
    return p


# ---------------- DTO helpers ----------------
def _order_summary(o: Order) -> dict:
    return {
        "id": o.id,
        "partner_id": o.partner_id,
        "order_id": getattr(o, "order_id", None),
        "trace_id": o.trace_id,
        "pipeline_status": o.pipeline_status.value if hasattr(o.pipeline_status, "value") else str(o.pipeline_status),
        "fulfillment_status": o.fulfillment_status.value if hasattr(o.fulfillment_status, "value") else str(o.fulfillment_status),
        "created_at": o.created_at,
        "fulfilled_at": getattr(o, "fulfilled_at", None),
    }


# ---------------- OTA payload parsing ----------------
def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    if "," in s:
        return [x.strip() for x in s.split(",") if x.strip()]
    return [s]

def _ts_from_iso(iso: str | None) -> int:
    if not iso:
        return int(time.time())
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return int(time.time())

def _extract_ota_payload(payload: dict) -> dict:
    """
    Expected OTA shape:
    {
      "order": {"id": "...", "accepted_at": "...", "callback_url": "..."},
      "customer": {"name": "...", "email": "..."},
      "items": [ { "name": "...", "qty": 1, "unit_price": 159430, "currency":"THB",
                   "product_id":"...", "variant_id":"...", "variant_name":"..." } ],
      "amounts": {"currency":"THB","subtotal":...,"total":...,"paid":...},
      "payment": {"processor":"...","method":"...","details":{...}}
    }
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    order = dict(payload.get("order") or {})
    customer = dict(payload.get("customer") or {})
    items = list(payload.get("items") or [])
    amounts = dict(payload.get("amounts") or {})
    payment = dict(payload.get("payment") or {})

    external_order_id = (order.get("id") or "").strip()
    accepted_at = (order.get("accepted_at") or "").strip()
    callback_url = (order.get("callback_url") or "").strip()

    currency = (
        (amounts.get("currency") or "").strip()
        or (items and (items[0].get("currency") or "").strip())
        or "THB"
    )

    total_amount = amounts.get("paid") or amounts.get("total") or amounts.get("subtotal")
    customer_name = (customer.get("name") or "").strip()
    emails = _as_list(customer.get("email"))
    customer_email = ", ".join(emails) if emails else None

    norm_items = []
    for idx, it in enumerate(items, start=1):
        norm_items.append({
            "line_no": idx,
            "product_id": it.get("product_id"),
            "product_name": it.get("name"),
            "variant_id": it.get("variant_id"),
            "variant_name": it.get("variant_name"),
            "unit_price": it.get("unit_price"),
            "currency": (it.get("currency") or currency),
            "quantity": it.get("qty") or it.get("quantity"),
            "meta": it,
        })

    return {
        "external_order_id": external_order_id,
        "accepted_at": accepted_at,
        "callback_url": callback_url,
        "currency": currency,
        "total_amount": total_amount,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "items": norm_items,
        "payment_processor": payment.get("processor"),
        "payment_method": payment.get("method"),
        "payment_details": payment.get("details") or {},
    }


# ---------------- API ----------------
@app.post("/orders")
async def create_order(
    request: Request,
    background: BackgroundTasks,
    partner_id: str = Header(..., alias="X-Partner-Id"),
    partner_token: str = Header(..., alias="X-Partner-Token"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    partner = await verify_partner(db, request, partner_id, partner_token)

    # Enforce IP allowlist **only here** (OTA ingress)
    try:
        enforce_ip_allowlist(request, getattr(partner, "allowlist_source_cidrs", None))
    except HTTPException as e:
        if e.status_code == 403:
            db.add(AuthEvent(
                partner_id=partner.partner_id,
                ip=_client_ip(request),
                reason=AuthFailReason.ip_block,
                user_agent=request.headers.get("user-agent"),
            ))
            db.commit()
        raise

    # Parse & normalize inbound OTA payload
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    parsed = _extract_ota_payload(raw)
    ext_order_id = parsed["external_order_id"]
    if not ext_order_id:
        raise HTTPException(status_code=400, detail="order.id required")

    callback_url = validate_callback_url(parsed["callback_url"], getattr(partner, "allowlist_domains", None))

    ts = _ts_from_iso(parsed.get("accepted_at"))
    trace_id = f"{partner.partner_id}-{ext_order_id}-{ts}"

    if idempotency_key:
        match = db.get(IdempotencyKey, (partner.partner_id, idempotency_key))
        if match:
            existing = db.get(Order, match.order_id)
            if existing:
                return _order_summary(existing)

    order = Order(
        partner_id=partner.partner_id,
        order_id=ext_order_id,
        trace_id=trace_id,
        ota_callback_url=callback_url,
        raw_ota_payload=raw,
        currency=(parsed.get("currency") or "THB"),
        total_amount=parsed.get("total_amount"),
        customer_name=parsed.get("customer_name"),
        customer_email=parsed.get("customer_email"),
        pipeline_status=PipelineStatus.accepted,
        fulfillment_status=FulfillmentStatus.unfulfilled,
        payment_processor=parsed.get("payment_processor"),
        payment_method=parsed.get("payment_method"),
        payment_details=parsed.get("payment_details") or {},
    )
    db.add(order); db.flush()

    for it in parsed["items"]:
        db.add(OrderItem(
            order_id=order.id,
            trace_id=trace_id,
            line_no=it.get("line_no"),
            product_id=it.get("product_id"),
            product_name=it.get("product_name"),
            variant_id=it.get("variant_id"),
            variant_name=it.get("variant_name"),
            unit_price=it.get("unit_price"),
            currency=it.get("currency") or order.currency,
            quantity=it.get("quantity"),
            meta=it.get("meta"),
        ))

    if idempotency_key:
        db.add(IdempotencyKey(
            partner_id=partner.partner_id,
            key=idempotency_key,
            order_id=order.id,
            trace_id=trace_id,
        ))

    db.commit(); db.refresh(order)
    background.add_task(process_ticketing, order.id)
    return _order_summary(order)


@app.get("/orders")
def list_orders(limit: int = 50, db: Session = Depends(get_db)):
    q: List[Order] = (
        db.query(Order)
        .order_by(Order.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [_order_summary(o) for o in q]

@app.get("/orders/{order_pk}")
def get_order(order_pk: int, db: Session = Depends(get_db)):
    o: Order | None = db.get(Order, order_pk)
    if not o:
        raise HTTPException(status_code=404, detail="not found")
    return {
        **_order_summary(o),
        "blocked": {"code": o.blocked_code, "reason": o.blocked_reason, "at": o.blocked_at},
        "payment": {
            "processor": o.payment_processor,
            "method": o.payment_method,
            "details": o.payment_details,
        },
        "items": [
            {
                "line_no": i.line_no,
                "product_id": i.product_id,
                "variant_id": i.variant_id,
                "product_name": i.product_name,
                "variant_name": i.variant_name,
                "currency": i.currency,
                "unit_price": i.unit_price,
                "qty": i.quantity,
            } for i in o.items
        ],
        "ticketing": {
            "status": getattr(o.ticketing_job, "status", None),
            "last_code": getattr(o.ticketing_job, "last_status_code", None),
            "attempts": [
                {"no": a.attempt_no, "code": a.status_code, "error": a.error, "at": a.created_at}
                for a in (o.ticketing_job.attempts if o.ticketing_job else [])
            ],
            "request_payload": getattr(o.ticketing_job, "request_payload", None),
            "response_payload": getattr(o.ticketing_job, "response_payload", None),
        },
        "ota_callback": {
            "status": getattr(o.ota_callback_job, "status", None),
            "last_code": getattr(o.ota_callback_job, "last_status_code", None),
            "delivered_at": getattr(o.ota_callback_job, "delivered_at", None),
            "attempts": [
                {"no": a.attempt_no, "code": a.status_code, "error": a.error, "at": a.created_at}
                for a in (o.ota_callback_job.attempts if o.ota_callback_job else [])
            ],
            "request_payload": getattr(o.ota_callback_job, "request_payload", None),
            "response_payload": getattr(o.ota_callback_job, "response_payload", None),
        },
    }


# ---------- Soraso callbacks (PUT updated payload; no token) ----------
def _apply_soraso_update(db: Session, o: Order, payload: dict, background: BackgroundTasks):
    """
    Accept Soraso's updated payload (any shape).
    - Save full payload to ticketing_job.response_payload (merge).
    - Update fulfillment fields if Status/FulfilledOn are present.
    - Ensure pipeline_status progresses to 'ticketed' on success.
    - Trigger OTA callback on success.
    """
    if o.ticketing_job:
        prev = o.ticketing_job.response_payload or {}
        merged = {**prev, **payload} if isinstance(prev, dict) and isinstance(payload, dict) else {"previous": prev, "current": payload}
        o.ticketing_job.response_payload = merged

    status_raw = str(payload.get("Status") or payload.get("status") or "").strip().lower()
    fulfilled_on = payload.get("FulfilledOn") or payload.get("fulfilled_on")

    if status_raw in ("fulfilled", "ticketed", "success", "ok"):
        o.fulfillment_status = FulfillmentStatus.fulfilled
        try:
            if isinstance(fulfilled_on, str) and fulfilled_on:
                o.fulfilled_at = datetime.fromisoformat(fulfilled_on.replace("Z", "+00:00"))
            else:
                o.fulfilled_at = func.now()
        except Exception:
            o.fulfilled_at = func.now()

        if o.pipeline_status != PipelineStatus.ticketed:
            o.pipeline_status = PipelineStatus.ticketed

        o.blocked_code = None
        o.blocked_reason = None
        o.blocked_at = None
        db.commit()

        try:
            from workers.ota_callback import process_ota_callback
            background.add_task(process_ota_callback, o.id)
        except Exception:
            pass

        return {"ok": True, "message": "updated and ticketed", **_order_summary(o)}

    code = payload.get("code")
    msg = payload.get("message") or payload.get("error")
    if code or msg:
        o.blocked_code = int(code) if str(code).isdigit() else None
        o.blocked_reason = (str(msg) if msg else "ticketing error")[:2000]
        o.blocked_at = func.now()
    db.commit()
    return {"ok": True, "message": "updated", **_order_summary(o)}

@app.put("/callbacks/soraso/orders/{partner_id}/{order_id}")
async def soraso_update_by_partner_order(
    partner_id: str,
    order_id: str,
    request: Request,
    background: BackgroundTasks,
    body: dict = Body(...),
    db: Session = Depends(get_db),
):
    o = (
        db.query(Order)
        .filter(Order.partner_id == partner_id, Order.order_id == order_id)
        .order_by(Order.id.desc())
        .first()
    )
    if not o:
        raise HTTPException(status_code=404, detail="order not found")
    return _apply_soraso_update(db, o, body, background)

@app.put("/callbacks/soraso/orders/by-trace/{trace_id}")
async def soraso_update_by_trace(
    trace_id: str,
    request: Request,
    background: BackgroundTasks,
    body: dict = Body(...),
    db: Session = Depends(get_db),
):
    o = db.query(Order).filter(Order.trace_id == trace_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="order not found")
    return _apply_soraso_update(db, o, body, background)


# ---------- Legacy partner-protected fulfillment (no IP allowlist) ----------
@app.put("/orders/{order_pk}/fulfill")
async def update_fulfill(
    order_pk: int,
    request: Request,
    partner_id: str = Header(..., alias="X-Partner-Id"),
    partner_token: str = Header(..., alias="X-Partner-Token"),
    db: Session = Depends(get_db),
):
    partner = await verify_partner(db, request, partner_id, partner_token)

    o: Order | None = db.get(Order, order_pk)
    if not o or o.partner_id != partner.partner_id:
        raise HTTPException(status_code=404, detail="not found")

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    status = (payload.get("status") or "").strip().lower()
    if status not in ("fulfilled", "success"):
        o.blocked_code = int(payload.get("code") or 400)
        o.blocked_reason = (payload.get("message") or payload.get("error") or "fulfillment failed")[:2000]
        o.blocked_at = func.now()
        db.commit()
        raise HTTPException(status_code=400, detail="unsupported status")

    o.fulfillment_status = FulfillmentStatus.fulfilled
    fulfilled_on = payload.get("FulfilledOn") or payload.get("fulfilled_on")
    if isinstance(fulfilled_on, str) and fulfilled_on:
        try:
            o.fulfilled_at = datetime.fromisoformat(fulfilled_on.replace("Z", "+00:00"))
        except Exception:
            o.fulfilled_at = func.now()
    else:
        o.fulfilled_at = func.now()
    o.blocked_code = None
    o.blocked_reason = None
    o.blocked_at = None
    if o.pipeline_status != PipelineStatus.ticketed:
        o.pipeline_status = PipelineStatus.ticketed
    db.commit()

    try:
        from workers.ota_callback import process_ota_callback
        background = BackgroundTasks()
        background.add_task(process_ota_callback, o.id)
    except Exception:
        pass

    return {**_order_summary(o), "message": "fulfillment recorded"}


# ---------- Partner Registration ----------
@app.post("/partners/register")
async def register_partner(payload: dict, db: Session = Depends(get_db)):
    if not isinstance(payload, dict) or not payload.get("company") or not payload.get("contactEmail"):
        raise HTTPException(status_code=400, detail="company and contactEmail required")

    rec = PartnerRegistration(
        reference=payload.get("reference"),
        company=payload.get("company"),
        website=payload.get("website"),
        country=payload.get("country"),
        address=payload.get("address"),
        taxId=payload.get("taxId"),
        contactName=payload.get("contactName"),
        contactEmail=payload.get("contactEmail"),
        contactPhone=payload.get("contactPhone"),
        techName=payload.get("techName"),
        techEmail=payload.get("techEmail"),
        techPhone=payload.get("techPhone"),
        vol=payload.get("vol"),
        rps=payload.get("rps"),
        launch=payload.get("launch"),
        tz=payload.get("tz"),
        desc=payload.get("desc"),
        auth=payload.get("auth"),
        env=payload.get("env") or "Sandbox",
        webhook=payload.get("webhook"),
        ips=payload.get("ips"),
        arch=payload.get("arch"),
        demo=payload.get("demo"),
        usecase=payload.get("usecase") or [],
        compliance=payload.get("compliance") or {},
        raw=payload,
    )
    db.add(rec); db.commit()
    return {"ok": True, "id": rec.id}


# ---------- (Optional) Debug: see detected client IP ----------
@app.get("/debug/ip")
def debug_ip(request: Request):
    return {
        "peer": request.client.host if request.client else None,
        "cf_connecting_ip": request.headers.get("cf-connecting-ip"),
        "x_forwarded_for": request.headers.get("x-forwarded-for"),
        "detected": _client_ip(request),
        "trusted_proxy_peer": _trusted_proxy_peer(request.client.host if request.client else ""),
    }
