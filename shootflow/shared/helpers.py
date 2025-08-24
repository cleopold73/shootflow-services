import logging
import time
from datetime import datetime, date
from typing import Callable, Optional, TypeVar, Any

def to_epoch_seconds(val, *, end_of_day: bool = False) -> int:
    if val is None:
        raise ValueError("Date value cannot be None")
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        # If naive, interpret as system local time
        return int(val.timestamp())
    if isinstance(val, date):
        dt = datetime(val.year, val.month, val.day, 23, 59, 59) if end_of_day else datetime(
            val.year, val.month, val.day, 0, 0, 0
        )
        return int(dt.timestamp())
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return int(s)
        try:
            dt = datetime.fromisoformat(s)
            return int(dt.timestamp())
        except Exception as e:
            raise ValueError(f"Unsupported date string format: {val!r}") from e
    raise TypeError(f"Unsupported date type: {type(val)!r}")

T = TypeVar("T")


def retry_with_backoff(
    func: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    multiplier: float = 2.0,
    logger: Optional[logging.Logger] = None,
    is_retryable: Optional[Callable[[Exception], bool]] = None,
    on_retry: Optional[str] = None,
) -> T:
    """
    Execute func() with exponential backoff on retryable exceptions.

    Args:
        func: Zero-arg callable to execute.
        max_attempts: Total attempts including the first.
        base_delay_s: Initial delay; grows by multiplier^(attempt-1).
        multiplier: Backoff multiplier.
        logger: Optional logger for retry messages.
        is_retryable: Predicate to decide if an exception should be retried. Defaults to retry all exceptions.
        on_retry: Short label to include in log messages.

    Returns:
        The return value of func().

    Raises:
        The last exception raised by func() if attempts are exhausted or exception is non-retryable.
    """
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as e:
            retry_ok = is_retryable(e) if callable(is_retryable) else True
            if (not retry_ok) or attempt >= attempts:
                raise
            delay = float(base_delay_s) * (multiplier ** (attempt - 1))
            if logger:
                label = on_retry or "operation"
                logger.warning(
                    "%s attempt %d failed (%s); retrying in %.1fs",
                    label,
                    attempt,
                    type(e).__name__,
                    delay,
                )
            time.sleep(delay)
    # Unreachable, but satisfies type checker
    raise RuntimeError("retry_with_backoff exhausted without returning or raising")