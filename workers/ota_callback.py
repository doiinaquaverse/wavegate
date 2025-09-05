import os, asyncio, time, hmac, hashlib, json
from datetime import datetime, timezone
import httpx
from sqlalchemy import func
from db import SessionLocal
from models import (
    Order, PipelineStatus,
    OtaCallbackJob, OtaCallbackAttempt, OtaCallbackJobStatus,
)
from retry_policy import jittered_offset, RETRY_SCHEDULE_OFFSETS

CALLBACK_SIGNING_SECRET = os.getenv("CALLBACK_SIGNING_SECRET")

def _canonical_json(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"))

def _signature(body: dict) -> str | None:
    if not CALLBACK_SIGNING_SECRET:
        return None
    raw = _canonical_json(body).encode("utf-8")
    return hmac.new(CALLBACK_SIGNING_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()

async def _post_callback(url: str, body: dict, headers: dict):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        hdrs = dict(headers)
        sig = _signature(body)
        if sig:
            hdrs["X-Callback-Signature"] = sig
        r = await client.post(url, json=body, headers=hdrs)
        return r.status_code, (r.text or "")

async def process_ota_callback(order_id: int):
    start = time.monotonic()
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return

        if order.pipeline_status in (PipelineStatus.ticketing_accepted,):
            order.pipeline_status = PipelineStatus.ota_callback_pending
            db.commit()

        job = order.ota_callback_job
        if job is None:
            job = OtaCallbackJob(
                order_id=order.id,
                trace_id=order.trace_id,
                callback_url=order.ota_callback_url,
                request_payload=order.raw_ota_payload,  # OTA's original payload
            )
            db.add(job); db.commit(); db.refresh(job)

        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": order.trace_id,
            "X-Origin-Partner-Id": order.partner_id,
            "X-External-Order-Id": order.order_id,
            "Idempotency-Key": order.trace_id,
        }

        for attempt in range(1, len(RETRY_SCHEDULE_OFFSETS) + 1):
            sleep_s = max(0.0, (start + jittered_offset(attempt)) - time.monotonic())
            if sleep_s:
                await asyncio.sleep(sleep_s)

            response_body = {
                "order_id": order.order_id,
                "status": "ticketed",
                "trace_id": order.trace_id,
                "ticketing_order_ref": order.ticketing_order_ref,
                "customer_email": order.customer_email,
                "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }

            code, text = None, ""
            try:
                code, text = await _post_callback(order.ota_callback_url, response_body, headers)
            except Exception as e:
                text = str(e)[:2000]

            job = db.get(OtaCallbackJob, job.id)
            db.add(OtaCallbackAttempt(
                ota_callback_job_id=job.id,
                trace_id=order.trace_id,
                attempt_no=attempt,
                status_code=code,
                error=None if (code and 200 <= code < 300) else (text or "")[:2000],
                duration_ms=None,
            ))
            job.last_status_code = code
            job.last_error = None if (code and 200 <= code < 300) else (text or "")[:2000]
            job.last_attempt_at = func.now()
            job.status = OtaCallbackJobStatus.in_progress
            job.request_payload = order.raw_ota_payload
            job.response_payload = response_body
            db.commit()

            if code and 200 <= code < 300:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ota_callback_delivered
                job.status = OtaCallbackJobStatus.delivered
                job.delivered_at = func.now()
                db.commit()
                return

            if code and 400 <= code < 500:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ota_callback_blocked
                order.blocked_code = code
                order.blocked_reason = (text or "")[:2000]
                order.blocked_at = func.now()
                job.status = OtaCallbackJobStatus.client_error
                db.commit()
                return

        job = db.get(OtaCallbackJob, job.id)
        job.status = OtaCallbackJobStatus.exhausted
        db.commit()
    finally:
        db.close()
