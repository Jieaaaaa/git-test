"""Deadline-aware policy helpers for unified model requests."""

import time


FINALIZATION_RESERVE_SECONDS = 3.0


def request_budget(deadline_at, now=None):
    current = time.monotonic() if now is None else now
    remaining = deadline_at - current - FINALIZATION_RESERVE_SECONDS
    if remaining <= 0:
        raise TimeoutError("no_model_budget_remaining")
    return remaining
