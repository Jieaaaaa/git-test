"""Deadline-aware policy helpers for unified model requests."""

import time


FINALIZATION_RESERVE_SECONDS = 8.0


def request_budget(deadline_at, now=None, fraction=1.0):
    current = time.monotonic() if now is None else now
    remaining = deadline_at - current - FINALIZATION_RESERVE_SECONDS
    if remaining <= 0:
        raise TimeoutError("no_model_budget_remaining")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    return remaining * fraction
