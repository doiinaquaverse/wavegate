# workers/ticketing.py
import asyncio, time
import httpx
from retry_policy import jittered_offset
from db import SessionLocal
from models import Order, PipelineStatus
from services.ticketing_format import build_ticketing_payload

TICKETING_API_URL = "https://ticketing.example.com/api/orders"  # TODO: env/config

async def _do_ticketing_request(payload: dict, headers: dict):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        r = await client.post(TICKETING_API_URL, json=payload, headers=headers)
        return r.status_code, r.text, (r.headers.get("X-Ticketing-Order-Ref") or "")

async def process_ticketing(order_id: int):
    """
    Run in background from main.py. Uses 15 attempts over ~5 minutes.
    On 2xx -> mark order ticketed and schedule OTA callback.
    On 4xx -> mark ticketing_blocked (permanent).
    On other errors -> keep retrying until the last attempt.
    """
    start = time.monotonic()

    # Load order fresh in this background context
    db = SessionLocal()
    try:
        order: Order | None = db.get(Order, order_id)
        if not order:
            return
        # Move to processing if not already
        if order.pipeline_status == PipelineStatus.accepted:
            order.pipeline_status = PipelineStatus.ticketing_processing
            db.commit()

        payload = build_ticketing_payload(order)
        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": order.trace_id,
            "X-Origin-Partner-Id": order.partner_id,
            "X-External-Order-Id": order.external_order_id,
            "Idempotency-Key": order.trace_id, 
        }

        for attempt in range(1, 16):  # 1..15
            # wait until this attempt's absolute offset
            sleep_s = max(0.0, (start + jittered_offset(attempt)) - time.monotonic())
            if sleep_s:
                await asyncio.sleep(sleep_s)

            t0 = time.monotonic()
            status_code, body_text, order_ref = None, None, ""

            try:
                status_code, body_text, order_ref = await _do_ticketing_request(payload, headers)
            except Exception as e:
                body_text = str(e)[:500]

            # Handle outcomes
            if status_code and 200 <= status_code < 300:
                # success → mark ticketed and stash reference if available
                order = db.get(Order, order_id)  # re-load
                order.pipeline_status = PipelineStatus.ticketed
                if order_ref:
                    order.ticketing_order_ref = order_ref
                # Optionally keep a snapshot of success body
                # order.ticketing_response_snapshot = { "body": body_text[:2000] }
                db.commit()

                # Schedule OTA callback phase
                from workers.ota_callback import process_ota_callback  # local import avoids cycles
                asyncio.create_task(process_ota_callback(order_id))
                return

            if status_code and 400 <= status_code < 500:
                # permanent block
                order = db.get(Order, order_id)
                order.pipeline_status = PipelineStatus.ticketing_blocked
                order.blocked_code = status_code
                order.blocked_reason = (body_text or "")[:2000]
                order.blocked_at = func.now()  # SQL function; let DB set tz-aware timestamp
                db.commit()
                return

            # else retriable (timeout/5xx/429/etc) → loop continues until attempt 15

        # After attempt 15 with no success → leave it in ticketing_processing (or set a soft marker if you prefer)
        # You can log something here if needed.

    finally:
        db.close()
