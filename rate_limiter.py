import time
import os
from fastapi import Header, HTTPException

RATE_LIMIT_CAPACITY = int(os.getenv("RATE_LIMIT_CAPACITY", "5"))
RATE_LIMIT_PER_SEC = float(os.getenv("RATE_LIMIT_PER_SEC", "1"))

class TokenBucket:
    __slots__ = ("capacity", "rate", "tokens", "ts")
    def __init__(self, capacity: int, rate: float):
        self.capacity = max(1, capacity)
        self.rate = max(0.01, rate)
        self.tokens = float(self.capacity)
        self.ts = time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.ts
        self.ts = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

_buckets = {}  # key: partner_id

async def partner_rate_limit(x_partner_id: str = Header(..., alias="X-Partner-Id")):
    b = _buckets.get(x_partner_id)
    if not b:
        b = _buckets[x_partner_id] = TokenBucket(RATE_LIMIT_CAPACITY, RATE_LIMIT_PER_SEC)
    if not b.allow(1.0):
        raise HTTPException(status_code=429, detail="Too Many Requests")
