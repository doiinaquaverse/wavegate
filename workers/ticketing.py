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
if not FORWARDING_PAYLOAD_URL:
    # If not set, we'll still try to run and naturally fail on request
    pass

async def _do_ticketing_request(payload: dict, headers: dict):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        r = await client.post(FORWARDING_PAYLOAD_URL, json=payload, headers=headers)
        return r.status_code, (r.text or ""), r.headers.get("X-Ticketing-Order-Ref", "")

async def process_ticketing(order_id: int):
    start = time.monotonic()
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return

        # Move to processing if not already
        if order.pipeline_status == PipelineStatus.accepted:
            order.pipeline_status = PipelineStatus.ticketing_processing
            db.commit()

        # Ensure a TicketingJob row exists (with initial request payload)
        job = order.ticketing_job
        if job is None:
            job = TicketingJob(
                order_id=order.id,
                trace_id=order.trace_id,
                request_payload=build_soraso_payload(order),
                status=TicketingJobStatus.queued,
            )
            db.add(job)
            db.commit()
            db.refresh(job)

        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": order.trace_id,
            "X-Origin-Partner-Id": order.partner_id,
            "X-External-Order-Id": order.order_id,
            "Idempotency-Key": order.trace_id,
        }

        for attempt in range(1, len(RETRY_SCHEDULE_OFFSETS) + 1):
            # Absolute schedule sleep
            sleep_s = max(0.0, (start + jittered_offset(attempt)) - time.monotonic())
            if sleep_s:
                await asyncio.sleep(sleep_s)

            code, body, order_ref = None, "", ""
            try:
                code, body, order_ref = await _do_ticketing_request(job.request_payload, headers)
            except Exception as e:
                body = str(e)[:2000]

            # Log attempt
            job = db.get(TicketingJob, job.id)
            db.add(TicketingAttempt(
                ticketing_job_id=job.id,
                trace_id=order.trace_id,
                attempt_no=attempt,
                status_code=code,
                error=None if (code and 200 <= code < 300) else (body or "")[:2000],
                duration_ms=None,  # optional
            ))
            job.last_status_code = code
            job.last_error = None if (code and 200 <= code < 300) else (body or "")[:2000]
            job.last_attempt_at = func.now()
            job.status = TicketingJobStatus.in_progress
            # store response payload if present
            if body:
                try:
                    job.response_payload = {"raw": body[:200000]}
                except Exception:
                    job.response_payload = None
            db.commit()

            if code and 200 <= code < 300 and body:
                # success with body → mark ticketed
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ticketed
                if order_ref:
                    order.ticketing_order_ref = order_ref
                job.status = TicketingJobStatus.ticketed
                db.commit()

                # create OTA job if not exists (preload request_payload with original OTA payload)
                if order.ota_callback_job is None:
                    db.add(OtaCallbackJob(
                        order_id=order.id,
                        trace_id=order.trace_id,
                        callback_url=order.ota_callback_url,
                        request_payload=order.raw_ota_payload,
                        status=None,  # will set in OTA worker
                    ))
                    db.commit()

                # schedule OTA callback
                from workers.ota_callback import process_ota_callback
                asyncio.create_task(process_ota_callback(order_id))
                return

            if code and 400 <= code < 500:
                # permanent block
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ticketing_blocked
                order.blocked_code = code
                order.blocked_reason = (body or "")[:2000]
                order.blocked_at = func.now()
                job = db.get(TicketingJob, job.id)
                job.status = TicketingJobStatus.client_error
                db.commit()
                return

            # otherwise retriable; loop continues

        # exhausted
        job = db.get(TicketingJob, job.id)
        job.status = TicketingJobStatus.exhausted
        db.commit()
    finally:
        db.close()
