# WaveGate

A small, reliable gateway that accepts orders from OTA partners, stores them, forwards to the ticketing system, and notifies the partner back when ticketing succeeds. It emphasizes **safety** (auth, IP allowlists, SSRF-safe callbacks), **reliability** (idempotency + disciplined retries), and **traceability** (single `trace_id` across the whole pipeline).

---

## Table of contents

* [Overview](#overview)
* [End-to-end flow](#end-to-end-flow)
* [API](#api)

  * [Authentication & Headers](#authentication--headers)
  * [POST /orders](#post-orders)
  * [GET /orders](#get-orders)
  * [GET /ordersid](#get-ordersid)
  * [GET /health](#get-health)
* [Idempotency](#idempotency)
* [Retry Policy](#retry-policy)
* [Security & Validation](#security--validation)
* [Data Model](#data-model)
* [Configuration](#configuration)
* [Local Development](#local-development)
* [Deployment Notes](#deployment-notes)
* [Observability](#observability)
* [Troubleshooting](#troubleshooting)
* [Roadmap](#roadmap)

---

## Overview

**App**: FastAPI service that exposes `/orders` and wires background workers that:

1. Map the OTA order into your **ticketing** format and POST it to `TICKETING_URL`.
2. On **2xx** from ticketing, POST an **OTA callback** to the partner’s `callback_url`.

**Statuses**:

* happy path: `accepted → ticketing_processing → ticketed → ota_callback_pending → ota_callback_delivered`
* blocked states on permanent 4xx: `ticketing_blocked`, `ota_callback_blocked`

**Traceability**: Each order has a server-generated **`trace_id`** (UUID) propagated outbound in headers and used as the **Idempotency-Key** for ticketing & callback.

---

## End-to-end flow

```
OTA → auth/allowlist check → accept+store → transform → send to ticketing
   → on 2xx, create tickets → send OTA callback → done
```

Concrete steps:

1. **Partner calls** `POST /orders` with auth headers + payload (includes `external_order_id`, `callback_url`, `items`, etc.). Optional inbound `Idempotency-Key` to avoid duplicates.
2. **Service validates** partner credentials, optional IP allowlist, per-partner rate limit, and SSRF-safe `callback_url`.
3. **Order is created** with a new `trace_id`. Items & raw payload are stored. `pipeline_status=accepted`.
4. **Ticketing worker** posts the mapped payload to `TICKETING_URL` (with headers including `Idempotency-Key: <trace_id>`). Retries follow the fixed schedule.
5. On **2xx** from ticketing, the order becomes `ticketed` and the **OTA callback worker** starts.
6. **OTA callback** is POSTed to the partner’s `callback_url` (optional HMAC signature). On 2xx, `pipeline_status=ota_callback_delivered`.

---

## API

Base URL (local dev): `http://localhost:8080`

### Authentication & Headers

Partners must include:

* `X-Partner-Id: <partner_id>`
* `X-Partner-Token: <partner_token>`
* `Idempotency-Key: <opaque-string>` (optional inbound; **recommended**)

Outbound headers we send to ticketing & callbacks:

* `X-Trace-Id: <trace_id>`
* `X-Origin-Partner-Id: <partner_id>`
* `X-External-Order-Id: <external_order_id>`
* `Idempotency-Key: <trace_id>`
* `X-Callback-Signature: <hex>` (optional; when `CALLBACK_SIGNING_SECRET` is set)

### POST /orders

Accepts a new order from a partner. **Async workers** handle ticketing + callbacks.

**Request (JSON):**

```json
{
  "external_order_id": "EXT-42",
  "callback_url": "https://partner.example.com/ota/callback",
  "items": [
    {"sku": "ADULT-DAY", "qty": 2, "unit_price_minor": 129900, "currency": "THB"}
  ],
  "buyer": {"name": "Alex"}
}
```

**Response (201):**

```json
{
  "id": 123,
  "partner_id": "demo",
  "external_order_id": "EXT-42",
  "trace_id": "c43d2c7c-...-b6a5",
  "pipeline_status": "accepted",
  "created_at": "2025-08-25T07:09:55.123Z"
}
```

**Errors**: `401` invalid partner/token, `403` IP not allowlisted, `429` rate limited, `400` invalid `callback_url`.

#### Example curl

```bash
curl -X POST http://localhost:8080/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Partner-Id: demo' \
  -H 'X-Partner-Token: demotoken' \
  -H 'Idempotency-Key: abc123' \
  -d '{
    "external_order_id":"EXT-42",
    "callback_url":"https://webhook.site/<your-id>",
    "items":[{"sku":"ADULT-DAY","qty":2,"unit_price_minor":129900,"currency":"THB"}],
    "buyer":{"name":"Alex"}
  }'
```

### GET /orders

Returns recent orders with status.

**Response:**

```json
[
  {"id":123, "partner_id":"demo", "external_order_id":"EXT-42", "trace_id":"...", "status":"ota_callback_delivered", "created_at":"..."}
]
```

### GET /orders/{id}

Detailed view of a single order, including attempts for ticketing & callback.

**Response (excerpt):**

```json
{
  "id": 123,
  "status": "ota_callback_delivered",
  "items": [{"sku":"ADULT-DAY","qty":2,"unit_price_minor":129900,"currency":"THB"}],
  "ticketing_job": {
    "status": "succeeded",
    "last_status_code": 200,
    "attempts": [
      {"no":1,"code":500,"error":null,"at":"..."},
      {"no":2,"code":500,"error":null,"at":"..."},
      {"no":3,"code":200,"error":null,"at":"..."}
    ]
  },
  "ota_callback_job": {
    "status": "succeeded",
    "last_status_code": 200,
    "attempts": [
      {"no":1,"code":200,"error":null,"at":"..."}
    ]
  }
}
```

### GET /health

Simple liveness probe.

---

## Idempotency

* **Inbound**: If a partner re-sends a request with the same `Idempotency-Key`, the gateway returns the original order instead of creating a duplicate. Mapping: `(partner_id, Idempotency-Key) → order_id`.
* **Outbound**: We send `Idempotency-Key: <trace_id>` to both ticketing and OTA callback endpoints.

---

## Retry Policy

Fixed 15 attempts over \~5 minutes:

```
[0, 2, 6, 12, 20, 30, 45, 65, 90, 120, 155, 195, 235, 270, 300] seconds (+ jitter)
```

Classification:

* `2xx` → success, stop.
* `4xx` (except 429) → **permanent** block, stop.
* `5xx` / timeout / `429` → retry until attempts are exhausted.

Same policy for **ticketing** and **OTA callback** workers.

---

## Security & Validation

* **Partner auth** via `X-Partner-Id` + `X-Partner-Token` (with optional hashing using `APP_HMAC_SECRET`).
* **Source IP allowlist** (per-partner CIDR ranges) optional.
* **SSRF-safe callbacks**: require `https`, default to **port 443**, host must resolve to **public** IPs; optional host allowlist via `ALLOWED_CALLBACK_HOSTS`.
* **Callback signing**: optional `X-Callback-Signature` (HMAC-SHA256 over canonical JSON) using `CALLBACK_SIGNING_SECRET`.
* **Per-partner rate limiting** (token bucket; capacity & rate configurable).

---

## Data Model

Tables (plus `partners`):

* `orders` — raw payload, denormalized fields, `pipeline_status`, `trace_id`, blocked info.
* `order_items` — line items (price in minor units).
* `ticketing_jobs` — job status & last result.
* `ticketing_attempts` — append-only attempt log.
* `ota_callback_jobs` — job status & last result.
* `ota_callback_attempts` — append-only attempt log.
* `idempotency_keys` — `(partner_id, key) → order_id` with timestamps.
* `auth_events` — auth/allowlist failures (reason, ip, optional UA).

---

## Configuration

Environment variables (with sensible defaults):

| Var                       | Purpose                                        | Default                          |
| ------------------------- | ---------------------------------------------- | -------------------------------- |
| `DATABASE_URL`            | SQLAlchemy URL (SQLite/Postgres)               | `sqlite:///./gateway.db`         |
| `APP_HMAC_SECRET`         | If set, seeds/stores **hashed** partner tokens | *(unset)*                        |
| `REQUIRE_HTTPS_CALLBACKS` | Enforce HTTPS callbacks                        | `1`                              |
| `ALLOWED_CALLBACK_HOSTS`  | Comma-separated host allowlist for callbacks   | *(unset)*                        |
| `CALLBACK_SIGNING_SECRET` | If set, sign callbacks with HMAC               | *(unset)*                        |
| `RATE_LIMIT_CAPACITY`     | Token bucket capacity per partner              | `5`                              |
| `RATE_LIMIT_PER_SEC`      | Token leak rate per second                     | `1`                              |
| `CALLBACK_CONCURRENCY`    | Concurrent OTA callbacks                       | `5`                              |
| `IDEMP_TTL_SECS`          | TTL for idempotency cache (informational)      | `86400`                          |
| `TICKETING_URL`           | Ticketing POST endpoint                        | `https://httpbin.org/status/200` |
| `PARTNERS_JSON`           | JSON list to seed partners                     | demo partner                     |

**Partner seeding** (`PARTNERS_JSON`):

```json
[
  {"id":"demo","name":"Demo Partner","token":"demotoken","allowlist_cidrs":["0.0.0.0/0"]}
]
```

If `APP_HMAC_SECRET` is set, the `token` will be stored hashed.

---

## Local Development

### Prereqs

```
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn sqlalchemy httpx python-dotenv pydantic ipaddress
```

### Run

```
export TICKETING_URL="https://httpbin.org/status/200"
uvicorn main:app --reload --host 0.0.0.0 --port 8080
```

### Smoke test

Create an order and watch workers run:

```bash
curl -X POST http://localhost:8080/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Partner-Id: demo' \
  -H 'X-Partner-Token: demotoken' \
  -H 'Idempotency-Key: abc123' \
  -d '{
    "external_order_id":"EXT-42",
    "callback_url":"https://webhook.site/<your-id>",
    "items":[{"sku":"ADULT-DAY","qty":2,"unit_price_minor":129900,"currency":"THB"}],
    "buyer":{"name":"Alex"}
  }'

curl http://localhost:8080/orders
curl http://localhost:8080/orders/1
```

---

## Deployment Notes

* **Split code** into packages and add **Alembic** migrations before production.
* **Process manager**: use `gunicorn`+`uvicorn` workers or `systemd` service.
* **Reverse proxy**: Nginx/Cloud LB terminating TLS; forward real client IP via `X-Forwarded-For`.
* **Environment**: set `REQUIRE_HTTPS_CALLBACKS=1`, and a strict `ALLOWED_CALLBACK_HOSTS` for partners.
* **Database**: Point `DATABASE_URL` to Postgres in prod.

Example `systemd` service (sketch):

```ini
[Unit]
Description=Aquaverse OTA Gateway
After=network.target

[Service]
Environment=DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/gateway
Environment=TICKETING_URL=https://ticketing.internal/api/orders
Environment=REQUIRE_HTTPS_CALLBACKS=1
Environment=ALLOWED_CALLBACK_HOSTS=partner.example.com
User=www-data
Group=www-data
WorkingDirectory=/srv/ota-gateway
ExecStart=/srv/ota-gateway/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080 --workers 4
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Observability

* **Trace ID** in logs/headers (`X-Trace-Id`).
* **Attempt logs** persisted in `*_attempts` tables for auditability.
* Consider adding structured JSON logs and OpenTelemetry in production.

---

## Troubleshooting

* `401 invalid partner`: check `PARTNERS_JSON` or correct `X-Partner-*` headers.
* `403 source IP not allowlisted`: adjust `allowlist_cidrs` for the partner.
* `400 callback_url must be https/port 443/public`: verify URL and DNS.
* `429 rate limited`: slow the request rate or raise `RATE_LIMIT_*` for tests.
* Ticketing stuck in retry: inspect `/orders/{id}` and your `TICKETING_URL` logs.

---

## Roadmap

* Alembic migrations & seed scripts.
* Admin endpoints (filter/search jobs & attempts).
* Structured logging + metrics/tracing.
* Optional async DB and a proper task queue (Celery/RQ) for scale.
* Unit/integration tests and load tests.

---

**License**: Internal use at Aquaverse.
