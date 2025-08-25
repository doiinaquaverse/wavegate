# retry_policy.py
import random

# 15 attempts within ~5 minutes (seconds from the first try)
RETRY_SCHEDULE_OFFSETS = [0, 2, 6, 12, 20, 30, 45, 65, 90, 120, 155, 195, 235, 270, 300]

def jittered_offset(attempt_no: int, *, max_offset: int = 300) -> float:
    """
    1-based attempt_no -> jittered absolute offset in seconds (<= max_offset).
    First attempt stays at 0s.
    """
    base = RETRY_SCHEDULE_OFFSETS[attempt_no - 1]
    if base == 0:
        return 0.0
    j = base * (0.8 + 0.4 * random.random())  # 0.8x..1.2x
    return min(j, max_offset)
