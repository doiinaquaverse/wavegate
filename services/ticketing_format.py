# services/ticketing_format.py
def build_ticketing_payload(order_row) -> dict:
    """
    Convert your stored raw OTA payload / denorm fields into the format the
    ticketing system expects.
    For now, pass through; adjust as needed.
    """
    return {
        "trace_id": order_row.trace_id,
        "external_order_id": order_row.external_order_id,
        "customer": {
            "name": order_row.customer_name,
            "email": order_row.customer_email,
        },
        "currency": order_row.currency or "THB",
        "total_amount_minor": order_row.total_amount_minor,
        "raw": order_row.raw_ota_payload,  # include if the ticketing side wants full context
    }
