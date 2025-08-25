from fastapi import FastAPI, Header, HTTPException, Depends, BackgroundTasks, Request
import os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from typing import Iterable, Set, Optional, List

from sqlalchemy.orm import Session
from sqlalchemy import func, select

from db import get_db
from models import (
    Order, OrderItem, Partner, PartnerStatus,
    IdempotencyKey, PipelineStatus,
)
from workers.ticketing import process_ticketing

import ipaddress, socket
from urllib.parse import urlparse

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "WaveGate")
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes", "on")
app = FastAPI(title=APP_NAME, debug=DEBUG)
bangkok_timezone = timezone(timedelta(hours=7))

FORWARDING_PAYLOAD_URL = os.getenv("FORWARDING_PAYLOAD_URL", "").strip()
if not FORWARDING_PAYLOAD_URL:
    # We don't call it from here, but ticketing worker needs it; fail early if missing.
    pass

# ---------------- Health ----------------
@app.get("/health")
async def health():
    return {
        "status": f"{APP_NAME} is healthy",
        "timestamp": datetime.now(bangkok_timezone).strftime("%Y-%m-%d %H:%M:%S"),
    }

# ---------------- Helpers: csv/ip/ssrf ----------------
def _csv_set(raw: str | None) -> Set[str]:
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            pass
    return request.client.host if request.client else ""

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

def enforce_ip_allowlist(request: Request, allowlist_source_cidrs: str | None) -> None:
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
    p: Partner | None = db.query(Partner).filter(Partner.partner_id == partner_id).first()
    if not p or p.status != PartnerStatus.active or p.partner_token != partner_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    enforce_ip_allowlist(request, getattr(p, "allowlist_source_cidrs", None))
    p.last_seen_at = func.now()
    db.add(p)
    db.commit()
    return p

# ---------------- DTO helpers ----------------
def _order_summary(o: Order) -> dict:
    return {
        "id": o.id,
        "partner_id": o.partner_id,
        "external_order_id": o.external_order_id,
        "trace_id": o.trace_id,
        "status": o.pipeline_status.value if hasattr(o.pipeline_status, "value") else str(o.pipeline_status),
        "created_at": o.created_at,
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

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Required fields
    external_order_id = (payload.get("external_order_id") or "").strip()
    if not external_order_id:
        raise HTTPException(status_code=400, detail="external_order_id required")

    callback_url = validate_callback_url(
        (payload.get("callback_url") or "").strip(),
        getattr(partner, "allowlist_domains", None),
    )

    # Inbound idempotency
    if idempotency_key:
        match = db.get(IdempotencyKey, {"partner_id": partner.partner_id, "key": idempotency_key})
        if match:
            existing = db.get(Order, match.order_id)
            if existing:
                return _order_summary(existing)

    # Create order
    order = Order(
        partner_id=partner.partner_id,
        external_order_id=external_order_id,
        ota_callback_url=callback_url,
        raw_ota_payload=payload,
        currency=payload.get("currency") or "THB",
        total_amount_minor=payload.get("total_amount_minor"),
        customer_name=(payload.get("buyer") or {}).get("name"),
        customer_email=(payload.get("buyer") or {}).get("email"),
        pipeline_status=PipelineStatus.accepted,
    )
    db.add(order)
    db.flush()  # get order.id and trace_id

    # Optional: items (best-effort mapping if provided)
    for idx, it in enumerate(payload.get("items") or [], start=1):
        db.add(OrderItem(
            order_id=order.id,
            line_no=idx,
            product_id=it.get("sku"),
            product_name=it.get("name"),
            unit_price_minor=it.get("unit_price_minor"),
            quantity=it.get("qty") or it.get("quantity"),
            line_total_minor=it.get("line_total_minor"),
            meta=it,
        ))

    # Save idempotency mapping if provided
    if idempotency_key:
        db.add(IdempotencyKey(partner_id=partner.partner_id, key=idempotency_key, order_id=order.id))

    db.commit()
    db.refresh(order)

    # Kick off the ticketing pipeline in background
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

@app.get("/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    o: Order | None = db.get(Order, order_id)
    if not o:
        raise HTTPException(status_code=404, detail="not found")
    return {
        **_order_summary(o),
        "blocked": {"code": o.blocked_code, "reason": o.blocked_reason, "at": o.blocked_at},
        "items": [
            {
                "line_no": i.line_no,
                "product_id": i.product_id,
                "quantity": i.quantity,
                "unit_price_minor": i.unit_price_minor,
                "line_total_minor": i.line_total_minor,
            }
            for i in o.items
        ],
        # Minimal job snapshots (you can expand later)
        "ticketing": {
            "status": getattr(o.ticketing_job, "status", None),
            "last_code": getattr(o.ticketing_job, "last_status_code", None),
            "attempts": [
                {"no": a.attempt_no, "code": a.status_code, "error": a.error, "at": a.created_at}
                for a in (o.ticketing_job.attempts if o.ticketing_job else [])
            ],
        },
        "ota_callback": {
            "status": getattr(o.ota_callback_job, "status", None),
            "last_code": getattr(o.ota_callback_job, "last_status_code", None),
            "attempts": [
                {"no": a.attempt_no, "code": a.status_code, "error": a.error, "at": a.created_at}
                for a in (o.ota_callback_job.attempts if o.ota_callback_job else [])
            ],
        },
    }
