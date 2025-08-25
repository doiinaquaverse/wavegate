import os, asyncio, time
import httpx
from sqlalchemy import func
from db import SessionLocal
from models import (
    Order, PipelineStatus,
    OtaCallbackJob, OtaCallbackAttempt, OtaCallbackJobStatus,
)
from retry_policy import jittered_offset, RETRY_SCHEDULE_OFFSETS

async def _post_callback(url: str, body: dict, headers: dict):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        r = await client.post(url, json=body, headers=headers)
        return r.status_code, (r.text or "")

async def process_ota_callback(order_id: int):
    start = time.monotonic()
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return

        # mark pending
        if order.pipeline_status == PipelineStatus.ticketed:
            order.pipeline_status = PipelineStatus.ota_callback_pending
            db.commit()

        job = order.ota_callback_job
        if job is None:
            job = OtaCallbackJob(
                order_id=order.id,
                trace_id=order.trace_id,
                callback_url=order.ota_callback_url,
                request_payload={},  # we fill just before sending
                status=OtaCallbackJobStatus.pending,
            )
            db.add(job)
            db.commit()
            db.refresh(job)

        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": order.trace_id,
            "X-Origin-Partner-Id": order.partner_id,
            "X-External-Order-Id": order.external_order_id,
            "Idempotency-Key": order.trace_id,
        }

        for attempt in range(1, len(RETRY_SCHEDULE_OFFSETS) + 1):
            sleep_s = max(0.0, (start + jittered_offset(attempt)) - time.monotonic())
            if sleep_s:
                await asyncio.sleep(sleep_s)

            # Prepare body each time (include current timestamp)
            body = {
                "external_order_id": order.external_order_id,
                "status": "ticketed",
                "order_id": order.id,
                "trace_id": order.trace_id,
                "ticketing_order_ref": order.ticketing_order_ref,
                "customer_email": order.customer_email,
                "at": datetime.utcnow().isoformat() + "Z",
            }
            code, text = None, ""
            try:
                code, text = await _post_callback(order.ota_callback_url, body, headers)
            except Exception as e:
                text = str(e)[:2000]

            # Log attempt
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
            job.request_payload = body
            db.commit()

            if code and 200 <= code < 300:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ota_callback_delivered
                job.status = OtaCallbackJobStatus.delivered
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

            # else retriable; continue

        # exhausted
        job = db.get(OtaCallbackJob, job.id)
        job.status = OtaCallbackJobStatus.exhausted
        db.commit()
    finally:
        db.close()
