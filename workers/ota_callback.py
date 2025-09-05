# workers/ota_callback.py  (FULL FILE)

import asyncio, time
from datetime import datetime
import httpx
from sqlalchemy import func, select
from db import SessionLocal
from models import (
    Order, PipelineStatus,
    OtaCallbackJob, OtaCallbackAttempt, OtaCallbackJobStatus,
)
from retry_policy import jittered_offset, RETRY_SCHEDULE_OFFSETS

def _val(x):
    return getattr(x, "value", x)

async def _post_callback(url: str, body: dict, headers: dict):
    # follow redirects to handle webhook endpoints that 302
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        r = await client.post(url, json=body, headers=headers)
        return r.status_code, (r.text or "")

async def process_ota_callback(order_id: int):
    start = time.monotonic()
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return

        # Idempotency guard: if already delivered, do nothing
        if _val(order.pipeline_status) == "ota_callback_delivered":
            return
        if order.ota_callback_job and _val(order.ota_callback_job.status) == "delivered":
            return

        # Ensure a job exists
        job = order.ota_callback_job
        if job is None:
            job = OtaCallbackJob(
                order_id=order.id,
                trace_id=order.trace_id,
                callback_url=order.ota_callback_url,
                request_payload=order.raw_ota_payload,  # OTA's original payload
                status=OtaCallbackJobStatus.pending,
            )
            db.add(job); db.commit(); db.refresh(order); job = order.ota_callback_job

        # Try to exclusively "claim" this job (row lock)
        try:
            job = (
                db.query(OtaCallbackJob)
                .filter(OtaCallbackJob.id == job.id)
                .with_for_update(nowait=True)
                .one()
            )
        except Exception:
            # another worker already claimed it
            return

        # mark as in-progress
        job.status = OtaCallbackJobStatus.in_progress
        db.commit()

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

            # Prepare response body for OTA (what we will send back)
            response_body = {
                "order_id": order.order_id,
                "status": "ticketed",
                "trace_id": order.trace_id,
                "customer_emails": order.customer_email,
                "at": datetime.utcnow().isoformat() + "Z",
            }

            code, text = None, ""
            try:
                code, text = await _post_callback(order.ota_callback_url, response_body, headers)
            except Exception as e:
                text = str(e)[:2000]

            # Compute the next attempt_no from DB to avoid unique collisions
            current_max = (
                db.query(func.coalesce(func.max(OtaCallbackAttempt.attempt_no), 0))
                .filter(OtaCallbackAttempt.ota_callback_job_id == job.id)
                .scalar()
            ) or 0
            attempt_no = current_max + 1

            # Log attempt
            db.add(OtaCallbackAttempt(
                ota_callback_job_id=job.id,
                trace_id=order.trace_id,
                attempt_no=attempt_no,
                status_code=code,
                error=None if (code and 200 <= code < 300) else (text or "")[:2000],
                duration_ms=None,
            ))
            job.last_status_code = code
            job.last_error = None if (code and 200 <= code < 300) else (text or "")[:2000]
            job.last_attempt_at = func.now()
            job.status = OtaCallbackJobStatus.in_progress
            job.request_payload = order.raw_ota_payload          # as requested
            job.response_payload = response_body                 # what we send back
            db.commit()

            if code and 200 <= code < 300:
                # mark delivered
                try:
                    # set pipeline_status to ota_callback_delivered via enum if possible
                    cur = getattr(order.pipeline_status, "value", order.pipeline_status)
                    if cur != "ota_callback_delivered":
                        try:
                            enum_cls = type(order.pipeline_status)
                            order.pipeline_status = enum_cls("ota_callback_delivered")
                        except Exception:
                            try:
                                # fallback import
                                from models import PipelineStatus as PS
                                order.pipeline_status = PS("ota_callback_delivered")
                            except Exception:
                                pass
                except Exception:
                    pass

                job.status = OtaCallbackJobStatus.delivered
                job.delivered_at = func.now()
                db.commit()
                return

            if code and 400 <= code < 500:
                # client error → block
                try:
                    # set pipeline_status to ota_callback_blocked via enum if possible
                    cur = getattr(order.pipeline_status, "value", order.pipeline_status)
                    if cur != "ota_callback_blocked":
                        try:
                            enum_cls = type(order.pipeline_status)
                            order.pipeline_status = enum_cls("ota_callback_blocked")
                        except Exception:
                            try:
                                from models import PipelineStatus as PS
                                order.pipeline_status = PS("ota_callback_blocked")
                            except Exception:
                                pass
                except Exception:
                    pass

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
