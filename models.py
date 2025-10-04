from __future__ import annotations
import enum
import uuid
from typing import Optional

from sqlalchemy import (
    String, Text, TIMESTAMP, Enum, func, Integer, BigInteger,
    ForeignKey, UniqueConstraint, JSON, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base

# ---------------- Enums ----------------

class PartnerStatus(enum.Enum):
    active = "active"
    terminated = "terminated"

class PipelineStatus(enum.Enum):
    accepted = "accepted"                     # order accepted by WaveGate
    ticketing_accepted = "ticketing_accepted" # Soraso 2xx received (not yet fulfilled)
    ota_callback_pending = "ota_callback_pending"
    ota_callback_delivered = "ota_callback_delivered"
    ticketing_blocked = "ticketing_blocked"
    ota_callback_blocked = "ota_callback_blocked"

class FulfillmentStatus(enum.Enum):
    unfulfilled = "unfulfilled"
    fulfilled = "fulfilled"

class TicketingJobStatus(enum.Enum):
    queued = "queued"
    in_progress = "in_progress"
    ticketed = "ticketed"     # Soraso accepted (2xx)
    client_error = "client_error"
    exhausted = "exhausted"

class OtaCallbackJobStatus(enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    delivered = "delivered"
    client_error = "client_error"
    exhausted = "exhausted"

class AuthFailReason(enum.Enum):
    # legacy
    no_partner = "no_partner"
    inactive = "inactive"
    bad_token = "bad_token"
    ip_block = "ip_block"
    # new granular reasons for /orders ingress
    ok = "ok"  # successful request to /orders
    missing_partner_id = "missing_partner_id"
    missing_partner_token = "missing_partner_token"
    invalid_json = "invalid_json"
    invalid_callback_url = "invalid_callback_url"
    callback_host_blocked = "callback_host_blocked"
    callback_domain_disallowed = "callback_domain_disallowed"
    rate_limited = "rate_limited"
    unknown = "unknown"

# ---------------- Partners ----------------

class Partner(Base):
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    partner_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    partner_name: Mapped[str] = mapped_column(String(200))
    partner_token: Mapped[str] = mapped_column(String(255))
    status: Mapped[PartnerStatus] = mapped_column(
        Enum(PartnerStatus, native_enum=False),
        default=PartnerStatus.active,
        nullable=False,
    )
    webhook: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))
    allowlist_domains: Mapped[Optional[str]] = mapped_column(Text)
    allowlist_source_cidrs: Mapped[Optional[str]] = mapped_column(Text)

# ---------------- Orders ----------------

class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("partner_id", "order_id", name="uq_partner_external_order"),
        Index("ix_orders_pipeline_status_created_at", "pipeline_status", "created_at"),
    )

    # Internal identity & tracing
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        index=True,
    )

    # Who and what
    partner_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("partners.partner_id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[str] = mapped_column(String(200), nullable=False)  # OTA's order id
    ota_callback_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Raw inbound payload (audit ground truth)
    raw_ota_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Denormalized business snapshot
    currency: Mapped[Optional[str]] = mapped_column(String(10), default="THB")
    total_amount: Mapped[Optional[int]] = mapped_column(BigInteger)
    customer_name: Mapped[Optional[str]] = mapped_column(String(255))
    customer_email: Mapped[Optional[str]] = mapped_column(String(320), index=True)
    accepted_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))

    # Pipeline & fulfillment
    pipeline_status: Mapped[PipelineStatus] = mapped_column(
        Enum(PipelineStatus, native_enum=False),
        default=PipelineStatus.accepted,
        nullable=False,
        index=True,
    )
    fulfillment_status: Mapped[FulfillmentStatus] = mapped_column(
        Enum(FulfillmentStatus, native_enum=False),
        default=FulfillmentStatus.unfulfilled,
        nullable=False,
        index=True,
    )
    fulfilled_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))

    # Ticketing success snapshot
    ticketing_order_ref: Mapped[Optional[str]] = mapped_column(String(200))
    ticketing_response_snapshot: Mapped[Optional[dict]] = mapped_column(JSON)

    # Permanent block (non-retriable 4xx) summary
    blocked_code: Mapped[Optional[int]] = mapped_column(Integer)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    blocked_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))

    # Payment
    payment_processor: Mapped[Optional[str]] = mapped_column(String(100))
    payment_method: Mapped[Optional[str]] = mapped_column(String(100))
    payment_details: Mapped[Optional[dict]] = mapped_column(JSON)

    # Timestamps
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # relationships
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan", lazy="selectin")
    ticketing_job: Mapped[Optional["TicketingJob"]] = relationship(back_populates="order", uselist=False, lazy="joined")
    ota_callback_job: Mapped[Optional["OtaCallbackJob"]] = relationship(back_populates="order", uselist=False, lazy="joined")
    idempotency_keys: Mapped[list["IdempotencyKey"]] = relationship(back_populates="order", cascade="all, delete-orphan", lazy="selectin")

# ---------------- OrderItems ----------------

class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (Index("ix_order_items_product_id", "product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", onupdate="RESTRICT", ondelete="CASCADE"), index=True, nullable=False)
    trace_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    line_no: Mapped[Optional[int]] = mapped_column(Integer)

    product_id: Mapped[Optional[str]] = mapped_column(String(200))
    product_name: Mapped[Optional[str]] = mapped_column(Text)
    variant_id: Mapped[Optional[str]] = mapped_column(String(200))
    variant_name: Mapped[Optional[str]] = mapped_column(Text)
    product_slug: Mapped[Optional[str]] = mapped_column(String(200))
    variant_slug: Mapped[Optional[str]] = mapped_column(String(200))
    variant_sku: Mapped[Optional[str]] = mapped_column(String(200))
    variant_image_url: Mapped[Optional[str]] = mapped_column(Text)

    unit_price: Mapped[Optional[int]] = mapped_column(BigInteger)  # minor units
    currency: Mapped[Optional[str]] = mapped_column(String(10))
    quantity: Mapped[Optional[int]] = mapped_column(Integer)

    meta: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")

# ---------------- TicketingJobs ----------------

class TicketingJob(Base):
    __tablename__ = "ticketing_jobs"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_ticketing_job_order"),
        Index("ix_ticketing_jobs_status_attempt", "status", "last_attempt_at"),
        Index("ix_ticketing_jobs_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_attempt_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))

    status: Mapped[TicketingJobStatus] = mapped_column(
        Enum(TicketingJobStatus, native_enum=False),
        default=TicketingJobStatus.queued,
        nullable=False,
        index=True,
    )

    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="ticketing_job")
    attempts: Mapped[list["TicketingAttempt"]] = relationship(back_populates="ticketing_job", cascade="all, delete-orphan", lazy="selectin")

# ---------------- TicketingAttempts ----------------

class TicketingAttempt(Base):
    __tablename__ = "ticketing_attempts"
    __table_args__ = (
        UniqueConstraint("ticketing_job_id", "attempt_no", name="uq_ticketing_attempt_no"),
        Index("ix_ticketing_attempts_job_created", "ticketing_job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticketing_job_id: Mapped[int] = mapped_column(Integer, ForeignKey("ticketing_jobs.id", onupdate="RESTRICT", ondelete="CASCADE"), index=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    ticketing_job: Mapped["TicketingJob"] = relationship(back_populates="attempts")

# ---------------- OtaCallbackJobs ----------------

class OtaCallbackJob(Base):
    __tablename__ = "ota_callback_jobs"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_ota_callback_job_order"),
        Index("ix_ota_callback_jobs_status_attempt", "status", "last_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)

    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON)

    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_attempt_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))
    delivered_at: Mapped[Optional[str]] = mapped_column(TIMESTAMP(timezone=True))

    status: Mapped[OtaCallbackJobStatus] = mapped_column(
        Enum(OtaCallbackJobStatus, native_enum=False),
        default=OtaCallbackJobStatus.pending,
        nullable=False,
        index=True,
    )

    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="ota_callback_job")
    attempts: Mapped[list["OtaCallbackAttempt"]] = relationship(back_populates="ota_callback_job", cascade="all, delete-orphan", lazy="selectin")

# ---------------- OtaCallbackAttempts ----------------

class OtaCallbackAttempt(Base):
    __tablename__ = "ota_callback_attempts"
    __table_args__ = (
        UniqueConstraint("ota_callback_job_id", "attempt_no", name="uq_ota_callback_attempt_no"),
        Index("ix_ota_callback_attempts_job_created", "ota_callback_job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ota_callback_job_id: Mapped[int] = mapped_column(Integer, ForeignKey("ota_callback_jobs.id", onupdate="RESTRICT", ondelete="CASCADE"), index=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    ota_callback_job: Mapped["OtaCallbackJob"] = relationship(back_populates="attempts")

# ---------------- IdempotencyKeys ----------------

class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("ix_idempotency_keys_order_id", "order_id"),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    partner_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("partners.partner_id", onupdate="RESTRICT", ondelete="RESTRICT"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(200), primary_key=True)

    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False)
    trace_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)

    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="idempotency_keys")

# ---------------- PartnerRegistration (restored) ----------------

class PartnerRegistration(Base):
    __tablename__ = "partners_registration"
    __table_args__ = (
        Index("ix_partner_reg_created_at", "created_at"),
        Index("ix_partner_reg_company", "company"),
        Index("ix_partner_reg_contact_email", "contactEmail"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Company info
    company: Mapped[Optional[str]] = mapped_column(String(255))
    website: Mapped[Optional[str]] = mapped_column(String(255))
    country: Mapped[Optional[str]] = mapped_column(String(128))
    address: Mapped[Optional[str]] = mapped_column(Text)
    taxId: Mapped[Optional[str]] = mapped_column(String(128))
    businessType: Mapped[Optional[str]] = mapped_column(String(128))

    # Business contact
    contactName: Mapped[Optional[str]] = mapped_column(String(255))
    contactEmail: Mapped[Optional[str]] = mapped_column(String(320))
    contactPhone: Mapped[Optional[str]] = mapped_column(String(64))

    # Technical contact
    techName: Mapped[Optional[str]] = mapped_column(String(255))
    techEmail: Mapped[Optional[str]] = mapped_column(String(320))
    techPhone: Mapped[Optional[str]] = mapped_column(String(64))

    # Integration details
    expectedVolume: Mapped[Optional[str]] = mapped_column(String(128))
    rps: Mapped[Optional[str]] = mapped_column(String(64))            # expected requests per second
    authMethod: Mapped[Optional[str]] = mapped_column(String(128))
    targetEnvironment: Mapped[Optional[str]] = mapped_column(String(64))
    callbackURL: Mapped[Optional[str]] = mapped_column(Text)
    ipAllowlist: Mapped[Optional[str]] = mapped_column(Text)
    websiteDomain: Mapped[Optional[str]] = mapped_column(String(255))

    # Raw payload from form
    raw: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[str] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

# ---------------- AuthEvents ----------------

class AuthEvent(Base):
    __tablename__ = "auth_events"
    __table_args__ = (
        Index("ix_auth_events_created_at", "created_at"),
        Index("ix_auth_events_partner_created", "partner_id", "created_at"),
        Index("ix_auth_events_reason_created", "reason", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    partner_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    reason: Mapped[AuthFailReason] = mapped_column(Enum(AuthFailReason, native_enum=False), nullable=False)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    # metadata captured by /orders ingress logging
    method: Mapped[Optional[str]] = mapped_column(String(10))
    path: Mapped[Optional[str]] = mapped_column(String(512))
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    headers: Mapped[Optional[dict]] = mapped_column(JSON)
    body: Mapped[Optional[dict]] = mapped_column(JSON)
    body_truncated: Mapped[Optional[bool]] = mapped_column()
