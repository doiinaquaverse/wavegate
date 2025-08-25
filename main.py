"""
Aquaverse OTA Gateway — single-file FastAPI app wired end-to-end

This file includes:
- DB models for 9 tables (incl. partners)
- POST /orders endpoint with partner auth, allowlist, idempotency, rate limiting
- Fixed retry policy workers for ticketing + OTA callback (async, httpx)
- SSRF-safe callback_url validation + optional host allowlist
- Optional callback signing (HMAC-SHA256) if CALLBACK_SIGNING_SECRET is set
- GET /orders, GET /orders/{id}, GET /health
- Simple transformation stub in services/ticketing_format.py form (inline here)

Run locally:
  pip install fastapi uvicorn sqlmodel sqlalchemy httpx python-dotenv pydantic ipaddress
  # or: pip install -r requirements.txt (see bottom for suggested contents)

  export TICKETING_URL="https://httpbin.org/status/200"  # replace with real ticketing endpoint
  uvicorn main:app --reload --host 0.0.0.0 --port 8080

Test:
  curl -X POST http://localhost:8080/orders \
    -H 'Content-Type: application/json' \
    -H 'X-Partner-Id: demo' \
    -H 'X-Partner-Token: demotoken' \
    -H 'Idempotency-Key: abc123' \
    -d '{
      "external_order_id": "EXT-42",
      "callback_url": "https://webhook.site/your-real-id",
      "items": [{"sku":"ADULT-DAY","qty":2,"unit_price_minor":129900,"currency":"THB"}],
      "buyer": {"name": "Alex"}
    }'

Notes:
- For demo, a default partner (id=demo, token=demotoken, allowlist=any) is seeded at startup unless PARTNERS_JSON is provided.
- SQLite is used by default. Change DATABASE_URL env to point to Postgres if desired.
- This is a single-file version for quick start. For production, split into packages and add Alembic, structured logging, metrics, etc.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import random
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from sqlalchemy import (
    JSON as SA_JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from dotenv import load_dotenv
import httpx

# -------------------------
# Config
# -------------------------
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gateway.db")
APP_HMAC_SECRET = os.getenv("APP_HMAC_SECRET")  # optional; if set, hashes partner tokens at seed time
REQUIRE_HTTPS_CALLBACKS = os.getenv("REQUIRE_HTTPS_CALLBACKS", "1") == "1"
ALLOWED_CALLBACK_HOSTS = set(
    [h.strip() for h in os.getenv("ALLOWED_CALLBACK_HOSTS", "").split(",") if h.strip()]
)
CALLBACK_SIGNING_SECRET = os.getenv("CALLBACK_SIGNING_SECRET")  # optional HMAC for callbacks

RATE_LIMIT_CAPACITY = int(os.getenv("RATE_LIMIT_CAPACITY", "5"))
RATE_LIMIT_PER_SEC = float(os.getenv("RATE_LIMIT_PER_SEC", "1"))
CALLBACK_CONCURRENCY = int(os.getenv("CALLBACK_CONCURRENCY", "5"))
IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS", str(24 * 3600)))

TICKETING_URL = os.getenv("TICKETING_URL", "https://httpbin.org/status/200")  # replace

# Retry policy (fixed schedule, ~5 mins total)
RETRY_OFFSETS = [0, 2, 6, 12, 20, 30, 45, 65, 90, 120, 155, 195, 235, 270, 300]

# -------------------------
# DB setup (SQLAlchemy)
# -------------------------
engine = create_engine(DATABASE_URL, future=True, echo=os.getenv("SQL_ECHO") == "1")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()

# -------------------------
# Models
# -------------------------
class Partner(Base):
    __tablename__ = "partners"
    id = Column(String(64), primary_key=True)
    name = Column(String(200), nullable=False)
    token = Column(String(256), nullable=False)  # plaintext or hashed
    allowlist_cidrs = Column(Text, nullable=True)  # JSON list of CIDRs
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())

    orders = relationship("Order", back_populates="partner")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    partner_id = Column(String(64), ForeignKey("partners.id"), nullable=False, index=True)
    external_order_id = Column(String(128), nullable=False)
    trace_id = Column(String(36), nullable=False, unique=True, index=True)
    callback_url = Column(String(2048), nullable=False)
    raw_payload = Column(SA_JSON, nullable=False)

    pipeline_status = Column(String(64), nullable=False, default="accepted", index=True)
    blocked_reason = Column(String(256), nullable=True)
    blocked_code = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())
    updated_at = Column(DateTime(timezone=True), default=lambda: now_utc(), onupdate=lambda: now_utc())

    partner = relationship("Partner", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    ticketing_job = relationship("TicketingJob", uselist=False, back_populates="order", cascade="all, delete-orphan")
    ota_callback_job = relationship("OTACallbackJob", uselist=False, back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("partner_id", "external_order_id", name="uq_partner_ext_order"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    sku = Column(String(128), nullable=False)
    qty = Column(Integer, nullable=False)
    unit_price_minor = Column(Integer, nullable=False)
    currency = Column(String(8), nullable=False)

    order = relationship("Order", back_populates="items")


class TicketingJob(Base):
    __tablename__ = "ticketing_jobs"
    order_id = Column(Integer, ForeignKey("orders.id"), primary_key=True)
    job_status = Column(String(64), nullable=False, default="pending", index=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())
    updated_at = Column(DateTime(timezone=True), default=lambda: now_utc(), onupdate=lambda: now_utc())

    order = relationship("Order", back_populates="ticketing_job")
    attempts = relationship("TicketingAttempt", back_populates="job", cascade="all, delete-orphan")


class TicketingAttempt(Base):
    __tablename__ = "ticketing_attempts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("ticketing_jobs.order_id"), index=True)
    attempt_no = Column(Integer, nullable=False)
    status_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())

    job = relationship("TicketingJob", back_populates="attempts")


class OTACallbackJob(Base):
    __tablename__ = "ota_callback_jobs"
    order_id = Column(Integer, ForeignKey("orders.id"), primary_key=True)
    job_status = Column(String(64), nullable=False, default="pending", index=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())
    updated_at = Column(DateTime(timezone=True), default=lambda: now_utc(), onupdate=lambda: now_utc())

    order = relationship("Order", back_populates="ota_callback_job")
    attempts = relationship("OTACallbackAttempt", back_populates="job", cascade="all, delete-orphan")


class OTACallbackAttempt(Base):
    __tablename__ = "ota_callback_attempts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("ota_callback_jobs.order_id"), index=True)
    attempt_no = Column(Integer, nullable=False)
    status_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())

    job = relationship("OTACallbackJob", back_populates="attempts")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    id = Column(Integer, primary_key=True, autoincrement=True)
    partner_id = Column(String(64), nullable=False, index=True)
    key = Column(String(200), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc(), index=True)

    __table_args__ = (
        UniqueConstraint("partner_id", "key", name="uq_partner_key"),
    )


class AuthEvent(Base):
    __tablename__ = "auth_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    partner_id = Column(String(64), nullable=True)
    reason = Column(String(200), nullable=False)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(256), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: now_utc())


# -------------------------
# Utils
# -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def json_canonical(obj: Any) -> bytes:
    """Canonical JSON for signing: UTF-8, no spaces, sorted keys."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def hash_token(token: str) -> str:
    if not APP_HMAC_SECRET:
        return token
    return hmac.new(APP_HMAC_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def check_token(provided: str, stored: str) -> bool:
    if not APP_HMAC_SECRET:
        return hmac.compare_digest(provided, stored)
    # stored is hash
    return hmac.compare_digest(
        hmac.new(APP_HMAC_SECRET.encode(), provided.encode(), hashlib.sha256).hexdigest(), stored
    )


def parse_client_ip(request: Request) -> str:
    # Trust X-Forwarded-For first hop if present; else use client.host
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def ip_in_cidrs(ip_str: str, cidrs: List[str]) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if ip_obj in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def is_public_resolvable_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
        for family, _, _, _, sockaddr in infos:
            if family == socket.AF_INET:
                ip = ipaddress.ip_address(sockaddr[0])
            elif family == socket.AF_INET6:
                ip = ipaddress.ip_address(sockaddr[0])
            else:
                continue
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def validate_callback_url(url: str) -> None:
    from urllib.parse import urlparse

    p = urlparse(url)
    if REQUIRE_HTTPS_CALLBACKS and p.scheme != "https":
        raise HTTPException(400, detail="callback_url must be https")
    # Enforce 443 unless explicitly allowed otherwise (policy)
    if p.port not in (None, 443):
        raise HTTPException(400, detail="callback_url must use port 443")
    if not p.hostname:
        raise HTTPException(400, detail="callback_url host missing")
    if ALLOWED_CALLBACK_HOSTS and p.hostname not in ALLOWED_CALLBACK_HOSTS:
        raise HTTPException(400, detail="callback_url host not allowed")
    if not is_public_resolvable_host(p.hostname):
        raise HTTPException(400, detail="callback_url host must resolve to public IP(s)")


# -------------------------
# Pydantic Schemas
# -------------------------
class OrderItemIn(BaseModel):
    sku: str
    qty: int = Field(gt=0)
    unit_price_minor: int = Field(ge=0)
    currency: str


class OrderCreate(BaseModel):
    external_order_id: str
    callback_url: str
    items: List[OrderItemIn] = Field(default_factory=list)
    # accept any additional keys as raw payload content
    class Config:
        extra = "allow"

    @validator("callback_url")
    def _valid_cb(cls, v):
        if not isinstance(v, str) or not v:
            raise ValueError("callback_url required")
        return v


class OrderOut(BaseModel):
    id: int
    partner_id: str
    external_order_id: str
    trace_id: str
    pipeline_status: str
    created_at: datetime


# -------------------------
# App + globals
# -------------------------
app = FastAPI(title="Aquaverse OTA Gateway")

# semaphores for outbound concurrency
callback_sema = asyncio.Semaphore(CALLBACK_CONCURRENCY)
# You can add a separate semaphore for ticketing if needed

# simple in-memory token-bucket per partner
class TokenBucket:
    def __init__(self, capacity: int, rate_per_sec: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.rate = rate_per_sec
        self.updated_at = time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

rate_limiters: Dict[str, TokenBucket] = {}


# Dependency for DB session

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------
# Seed / init
# -------------------------
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    seed_partners()


def seed_partners():
    """Seed partners from PARTNERS_JSON or default demo."""
    raw = os.getenv("PARTNERS_JSON")
    partners: List[Dict[str, Any]]
    if raw:
        try:
            partners = json.loads(raw)
        except Exception as e:
            print(f"Invalid PARTNERS_JSON: {e}")
            partners = []
    else:
        partners = [
            {
                "id": "demo",
                "name": "Demo Partner",
                "token": "demotoken",
                "allowlist_cidrs": [],
            }
        ]
    with SessionLocal() as db:
        for p in partners:
            pid = p.get("id")
            if not pid:
                continue
            exists = db.execute(select(Partner).where(Partner.id == pid)).scalar_one_or_none()
            token = p.get("token") or uuid.uuid4().hex
            hashed = hash_token(token)
            allow = p.get("allowlist_cidrs") or []
            if not exists:
                db.add(
                    Partner(
                        id=pid,
                        name=p.get("name") or pid,
                        token=hashed,
                        allowlist_cidrs=json.dumps(allow),
                    )
                )
                db.commit()


# -------------------------
# Auth helper
# -------------------------

def authenticate(db: Session, request: Request, partner_id: str, partner_token: str) -> Partner:
    partner = db.execute(select(Partner).where(Partner.id == partner_id)).scalar_one_or_none()
    client_ip = parse_client_ip(request)
    if not partner:
        db.add(AuthEvent(partner_id=partner_id, reason="unknown_partner", ip=client_ip, user_agent=request.headers.get("user-agent")))
        db.commit()
        raise HTTPException(401, detail="invalid partner")
    if not check_token(partner_token, partner.token):
        db.add(AuthEvent(partner_id=partner_id, reason="bad_token", ip=client_ip, user_agent=request.headers.get("user-agent")))
        db.commit()
        raise HTTPException(401, detail="invalid credentials")

    # Allowlist check if configured
    if partner.allowlist_cidrs:
        try:
            cidrs = json.loads(partner.allowlist_cidrs)
        except Exception:
            cidrs = []
        if cidrs and not ip_in_cidrs(client_ip, cidrs):
            db.add(AuthEvent(partner_id=partner_id, reason="ip_not_allowlisted", ip=client_ip, user_agent=request.headers.get("user-agent")))
            db.commit()
            raise HTTPException(403, detail="source IP not allowlisted")

    # Rate limiting per partner
    limiter = rate_limiters.setdefault(partner.id, TokenBucket(RATE_LIMIT_CAPACITY, RATE_LIMIT_PER_SEC))
    if not limiter.take():
        raise HTTPException(429, detail="rate limited")

    return partner


# -------------------------
# Transformation stub
# -------------------------

def map_ota_to_ticketing(raw_order: Dict[str, Any]) -> Dict[str, Any]:
    """Map OTA payload to ticketing provider format. Adjust as needed."""
    items = raw_order.get("items", [])
    return {
        "orderRef": raw_order.get("external_order_id"),
        "customer": raw_order.get("buyer", {}),
        "lines": [
            {
                "sku": it.get("sku"),
                "qty": it.get("qty"),
                "amountMinor": it.get("unit_price_minor"),
                "currency": it.get("currency", "THB"),
            }
            for it in items
        ],
    }


# -------------------------
# Workers (ticketing & callback)
# -------------------------
async def run_with_retries(
    label: str,
    do_attempt,
    on_success,
    on_permanent,
    on_retry,
    jitter: float = 0.2,
):
    for attempt_no, base_delay in enumerate(RETRY_OFFSETS, start=1):
        if base_delay > 0:
            await asyncio.sleep(base_delay + random.uniform(0, jitter))
        status_code = None
        error = None
        start = time.monotonic()
        try:
            status_code = await do_attempt(attempt_no)
            duration_ms = int((time.monotonic() - start) * 1000)
        except Exception as e:
            error = str(e)
            duration_ms = int((time.monotonic() - start) * 1000)
        # classify
        if status_code is not None and 200 <= status_code < 300:
            await on_success(attempt_no, status_code, duration_ms)
            return
        elif status_code is not None and 400 <= status_code < 500 and status_code != 429:
            await on_permanent(attempt_no, status_code, duration_ms, error)
            return
        else:
            # 5xx/timeout/None/429
            cont = await on_retry(attempt_no, status_code, duration_ms, error)
            if not cont:
                return


async def process_ticketing(order_id: int):
    async with httpx.AsyncClient(timeout=10.0) as client:
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            if not order:
                return
            job = order.ticketing_job or TicketingJob(order_id=order.id)
            order.pipeline_status = "ticketing_processing"
            db.add(job)
            db.commit()

        async def do_attempt(attempt_no: int) -> Optional[int]:
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return 499  # aborted
                payload = map_ota_to_ticketing(order.raw_payload)
                headers = {
                    "X-Trace-Id": order.trace_id,
                    "X-Origin-Partner-Id": order.partner_id,
                    "X-External-Order-Id": order.external_order_id,
                    "Idempotency-Key": order.trace_id,
                }
            resp = await client.post(TICKETING_URL, json=payload, headers=headers)
            return resp.status_code

        async def on_success(attempt_no, status_code, duration_ms):
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return
                job = order.ticketing_job
                job.job_status = "succeeded"
                job.last_status_code = status_code
                job.attempt_count = attempt_no
                db.add(TicketingAttempt(order_id=order.id, attempt_no=attempt_no, status_code=status_code, duration_ms=duration_ms))
                order.pipeline_status = "ticketed"
                db.commit()
            # schedule OTA callback
            asyncio.create_task(process_ota_callback(order_id))

        async def on_permanent(attempt_no, status_code, duration_ms, error):
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return
                job = order.ticketing_job
                job.job_status = "blocked"
                job.last_status_code = status_code
                job.last_error = error
                job.attempt_count = attempt_no
                db.add(TicketingAttempt(order_id=order.id, attempt_no=attempt_no, status_code=status_code, error=error, duration_ms=duration_ms))
                order.pipeline_status = "ticketing_blocked"
                order.blocked_code = status_code
                order.blocked_reason = "ticketing 4xx"
                db.commit()

        async def on_retry(attempt_no, status_code, duration_ms, error) -> bool:
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return False
                job = order.ticketing_job
                job.job_status = "retrying"
                job.last_status_code = status_code
                job.last_error = error
                job.attempt_count = attempt_no
                db.add(TicketingAttempt(order_id=order.id, attempt_no=attempt_no, status_code=status_code, error=error, duration_ms=duration_ms))
                db.commit()
            return attempt_no < len(RETRY_OFFSETS)

        await run_with_retries("ticketing", do_attempt, on_success, on_permanent, on_retry)


async def process_ota_callback(order_id: int):
    async with httpx.AsyncClient(timeout=10.0) as client:
        with SessionLocal() as db:
            order = db.get(Order, order_id)
            if not order:
                return
            # move status to pending if not blocked
            if order.pipeline_status == "ticketed":
                order.pipeline_status = "ota_callback_pending"
                db.commit()
            job = order.ota_callback_job or OTACallbackJob(order_id=order.id)
            db.add(job)
            db.commit()

        async def do_attempt(attempt_no: int) -> Optional[int]:
            async with callback_sema:
                with SessionLocal() as db:
                    order = db.get(Order, order_id)
                    if not order:
                        return 499
                    payload = {
                        "order_id": order.id,
                        "trace_id": order.trace_id,
                        "external_order_id": order.external_order_id,
                        "status": "ticketed",
                        "at": now_utc().isoformat(),
                    }
                    headers = {
                        "X-Trace-Id": order.trace_id,
                        "X-Origin-Partner-Id": order.partner_id,
                        "X-External-Order-Id": order.external_order_id,
                        "Idempotency-Key": order.trace_id,
                    }
                    if CALLBACK_SIGNING_SECRET:
                        sig = hmac.new(CALLBACK_SIGNING_SECRET.encode(), json_canonical(payload), hashlib.sha256).hexdigest()
                        headers["X-Callback-Signature"] = sig
                resp = await client.post(order.callback_url, json=payload, headers=headers)
                return resp.status_code

        async def on_success(attempt_no, status_code, duration_ms):
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return
                job = order.ota_callback_job
                job.job_status = "succeeded"
                job.last_status_code = status_code
                job.attempt_count = attempt_no
                db.add(OTACallbackAttempt(order_id=order.id, attempt_no=attempt_no, status_code=status_code, duration_ms=duration_ms))
                order.pipeline_status = "ota_callback_delivered"
                db.commit()

        async def on_permanent(attempt_no, status_code, duration_ms, error):
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return
                job = order.ota_callback_job
                job.job_status = "blocked"
                job.last_status_code = status_code
                job.last_error = error
                job.attempt_count = attempt_no
                db.add(OTACallbackAttempt(order_id=order.id, attempt_no=attempt_no, status_code=status_code, error=error, duration_ms=duration_ms))
                order.pipeline_status = "ota_callback_blocked"
                order.blocked_code = status_code
                order.blocked_reason = "callback 4xx"
                db.commit()

        async def on_retry(attempt_no, status_code, duration_ms, error) -> bool:
            with SessionLocal() as db:
                order = db.get(Order, order_id)
                if not order:
                    return False
                job = order.ota_callback_job
                job.job_status = "retrying"
                job.last_status_code = status_code
                job.last_error = error
                job.attempt_count = attempt_no
                db.add(OTACallbackAttempt(order_id=order.id, attempt_no=attempt_no, status_code=status_code, error=error, duration_ms=duration_ms))
                db.commit()
            return attempt_no < len(RETRY_OFFSETS)

        await run_with_retries("ota_callback", do_attempt, on_success, on_permanent, on_retry)


# -------------------------
# Endpoints
# -------------------------
@app.get("/health")
def health():
    return {"ok": True, "time": now_utc().isoformat()}


@app.get("/orders")
def list_orders(limit: int = 50, db: Session = Depends(get_db)):
    q = db.execute(select(Order).order_by(Order.id.desc()).limit(min(limit, 200))).scalars().all()
    return [
        {
            "id": o.id,
            "partner_id": o.partner_id,
            "external_order_id": o.external_order_id,
            "trace_id": o.trace_id,
            "status": o.pipeline_status,
            "created_at": o.created_at,
        }
        for o in q
    ]


@app.get("/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, detail="not found")
    return {
        "id": o.id,
        "partner_id": o.partner_id,
        "external_order_id": o.external_order_id,
        "trace_id": o.trace_id,
        "status": o.pipeline_status,
        "blocked": {"code": o.blocked_code, "reason": o.blocked_reason},
        "items": [
            {"sku": i.sku, "qty": i.qty, "unit_price_minor": i.unit_price_minor, "currency": i.currency}
            for i in o.items
        ],
        "ticketing_job": {
            "status": (o.ticketing_job.job_status if o.ticketing_job else None),
            "last_status_code": (o.ticketing_job.last_status_code if o.ticketing_job else None),
            "attempts": [
                {"no": a.attempt_no, "code": a.status_code, "error": a.error, "at": a.created_at}
                for a in (o.ticketing_job.attempts if o.ticketing_job else [])
            ],
        },
        "ota_callback_job": {
            "status": (o.ota_callback_job.job_status if o.ota_callback_job else None),
            "last_status_code": (o.ota_callback_job.last_status_code if o.ota_callback_job else None),
            "attempts": [
                {"no": a.attempt_no, "code": a.status_code, "error": a.error, "at": a.created_at}
                for a in (o.ota_callback_job.attempts if o.ota_callback_job else [])
            ],
        },
    }


@app.post("/orders", response_model=OrderOut)
async def create_order(
    request: Request,
    body: OrderCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    x_partner_id: str = Header(..., alias="X-Partner-Id"),
    x_partner_token: str = Header(..., alias="X-Partner-Token"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # Auth + rate limit + allowlist
    partner = authenticate(db, request, x_partner_id, x_partner_token)

    # Validate callback URL SSRF-safely
    validate_callback_url(body.callback_url)

    # Idempotency: if key provided and exists, return existing order
    existing_order: Optional[Order] = None
    if idempotency_key:
        row = db.execute(
            select(IdempotencyKey).where(IdempotencyKey.partner_id == partner.id, IdempotencyKey.key == idempotency_key)
        ).scalar_one_or_none()
        if row:
            existing_order = db.get(Order, row.order_id)
            if existing_order:
                return OrderOut(
                    id=existing_order.id,
                    partner_id=existing_order.partner_id,
                    external_order_id=existing_order.external_order_id,
                    trace_id=existing_order.trace_id,
                    pipeline_status=existing_order.pipeline_status,
                    created_at=existing_order.created_at,
                )

    # Create order
    trace_id = str(uuid.uuid4())
    order = Order(
        partner_id=partner.id,
        external_order_id=body.external_order_id,
        trace_id=trace_id,
        callback_url=body.callback_url,
        raw_payload=json.loads(body.json()),  # store submitted body as raw JSON
        pipeline_status="accepted",
    )
    db.add(order)
    db.flush()

    # Items
    for it in body.items:
        db.add(
            OrderItem(
                order_id=order.id,
                sku=it.sku,
                qty=it.qty,
                unit_price_minor=it.unit_price_minor,
                currency=it.currency,
            )
        )

    # Ticketing job placeholder
    db.add(TicketingJob(order_id=order.id, job_status="pending"))
    db.add(OTACallbackJob(order_id=order.id, job_status="pending"))

    # Persist idempotency mapping if key provided
    if idempotency_key:
        try:
            db.add(IdempotencyKey(partner_id=partner.id, key=idempotency_key, order_id=order.id))
        except IntegrityError:
            db.rollback()
            # rare race: load and return existing mapping
            row = db.execute(
                select(IdempotencyKey).where(IdempotencyKey.partner_id == partner.id, IdempotencyKey.key == idempotency_key)
            ).scalar_one_or_none()
            if row:
                existing = db.get(Order, row.order_id)
                return OrderOut(
                    id=existing.id,
                    partner_id=existing.partner_id,
                    external_order_id=existing.external_order_id,
                    trace_id=existing.trace_id,
                    pipeline_status=existing.pipeline_status,
                    created_at=existing.created_at,
                )

    db.commit()

    # Kick off ticketing worker (async)
    asyncio.create_task(process_ticketing(order.id))

    return OrderOut(
        id=order.id,
        partner_id=order.partner_id,
        external_order_id=order.external_order_id,
        trace_id=order.trace_id,
        pipeline_status=order.pipeline_status,
        created_at=order.created_at,
    )


# -------------------------
# Suggested requirements.txt
# -------------------------
# fastapi
# uvicorn
# sqlalchemy
# sqlmodel  # optional convenience (not used directly here, but handy if you refactor)
# httpx
# python-dotenv
# pydantic
# """ END OF FILE """
