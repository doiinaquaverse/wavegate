# workers/ticketing.py
import os, asyncio, time
import httpx
from sqlalchemy import func
from db import SessionLocal
from models import (
    Order, PipelineStatus,
    TicketingJob, TicketingAttempt, TicketingJobStatus,
    OtaCallbackJob,
)
from services.ticketing_format import build_soraso_payload
from retry_policy import jittered_offset, RETRY_SCHEDULE_OFFSETS

FORWARDING_PAYLOAD_URL = os.getenv("FORWARDING_PAYLOAD_URL", "").strip()
ACCEPT_ANY_2XX = os.getenv("TICKETING_ACCEPT_ANY_2XX", "0").lower() in ("1","true","yes","on")
AUTH_NAME = os.getenv("TICKETING_AUTH_HEADER_NAME")
AUTH_VALUE = os.getenv("TICKETING_AUTH_HEADER_VALUE")

async def _do_ticketing_request(payload: dict, headers: dict):
    # longer timeout + follow redirects
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        r = await client.post(FORWARDING_PAYLOAD_URL, json=payload, headers=headers)
        # keep header for order ref if present
        return r.status_code, (r.text or ""), r.headers.get("X-Ticketing-Order-Ref", "")

async def process_ticketing(order_id: int):
    start = time.monotonic()
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return

        if order.pipeline_status == PipelineStatus.accepted:
            order.pipeline_status = PipelineStatus.ticketing_processing
            db.commit()

        job = order.ticketing_job
        if job is None:
            job = TicketingJob(
                order_id=order.id,
                trace_id=order.trace_id,
                request_payload=build_soraso_payload(order),
                status=TicketingJobStatus.queued,
            )
            db.add(job); db.commit(); db.refresh(job)

        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": order.trace_id,
            "X-Origin-Partner-Id": order.partner_id,
            "X-External-Order-Id": order.order_id,
            "Idempotency-Key": order.trace_id,
        }
        if AUTH_NAME and AUTH_VALUE:
            headers[AUTH_NAME] = AUTH_VALUE

        for attempt in range(1, len(RETRY_SCHEDULE_OFFSETS) + 1):
            sleep_s = max(0.0, (start + jittered_offset(attempt)) - time.monotonic())
            if sleep_s:
                await asyncio.sleep(sleep_s)

            code, body, order_ref = None, "", ""
            try:
                code, body, order_ref = await _do_ticketing_request(job.request_payload, headers)
            except Exception as e:
                body = str(e)[:2000]

            job = db.get(TicketingJob, job.id)
            db.add(TicketingAttempt(
                ticketing_job_id=job.id,
                trace_id=order.trace_id,
                attempt_no=attempt,
                status_code=code,
                error=None if (code and 200 <= code < 300) else (body or "")[:2000],
                duration_ms=None,
            ))
            job.last_status_code = code
            job.last_error = None if (code and 200 <= code < 300) else (body or "")[:2000]
            job.last_attempt_at = func.now()
            job.status = TicketingJobStatus.in_progress
            if body:
                job.response_payload = {"raw": body[:200000]}
            db.commit()

            success = bool(code and 200 <= code < 300 and (ACCEPT_ANY_2XX or body or order_ref))
            if success:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ticketed
                if order_ref:
                    order.ticketing_order_ref = order_ref
                job.status = TicketingJobStatus.ticketed
                db.commit()

                if order.ota_callback_job is None:
                    db.add(OtaCallbackJob(
                        order_id=order.id,
                        trace_id=order.trace_id,
                        callback_url=order.ota_callback_url,
                        request_payload=order.raw_ota_payload,
                        status=None,
                    ))
                    db.commit()

                from workers.ota_callback import process_ota_callback
                asyncio.create_task(process_ota_callback(order_id))
                return

            if code and 400 <= code < 500:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ticketing_blocked
                order.blocked_code = code
                order.blocked_reason = (body or "")[:2000]
                order.blocked_at = func.now()
                job = db.get(TicketingJob, job.id)
                job.status = TicketingJobStatus.client_error
                db.commit()
                return

        # exhausted → make it visible on the order
        job = db.get(TicketingJob, job.id)
        job.status = TicketingJobStatus.exhausted
        order = db.get(Order, order_id)
        order.pipeline_status = PipelineStatus.ticketing_blocked
        order.blocked_code = job.last_status_code or 599
        order.blocked_reason = (job.last_error or "ticketing exhausted")[:2000]
        order.blocked_at = func.now()
        db.commit()
    finally:
        db.close()
