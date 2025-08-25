# workers/ota_callback.py
import asyncio, time
import httpx
from retry_policy import jittered_offset
from db import SessionLocal
from models import Order, PipelineStatus

async def _post_callback(url: str, body: dict, headers: dict):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        r = await client.post(url, json=body, headers=headers)
        return r.status_code, r.text

async def process_ota_callback(order_id: int):
    start = time.monotonic()
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return

        # move to callback_pending if needed
        if order.pipeline_status == PipelineStatus.ticketed:
            order.pipeline_status = PipelineStatus.ota_callback_pending
            db.commit()

        callback_url = order.ota_callback_url
        body = {
            "external_order_id": order.external_order_id,
            "status": "ticketed",
            "order_id": order.id,
            "trace_id": order.trace_id,
            "ticketing_order_ref": order.ticketing_order_ref,
            "customer_email": order.customer_email,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": order.trace_id,
            "X-Origin-Partner-Id": order.partner_id,
            "X-External-Order-Id": order.external_order_id,
            "Idempotency-Key": order.trace_id,  # ← add this
        }

        for attempt in range(1, 16):
            sleep_s = max(0.0, (start + jittered_offset(attempt)) - time.monotonic())
            if sleep_s:
                await asyncio.sleep(sleep_s)

            try:
                status, text = await _post_callback(callback_url, body, headers)
            except Exception as e:
                status, text = None, str(e)[:500]

            if status and 200 <= status < 300:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ota_callback_delivered
                db.commit()
                return

            if status and 400 <= status < 500:
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ota_callback_blocked
                order.blocked_code = status
                order.blocked_reason = (text or "")[:2000]
                order.blocked_at = func.now()
                db.commit()
                return

            # else retriable → keep looping until attempt 15; then stop
    finally:
        db.close()
