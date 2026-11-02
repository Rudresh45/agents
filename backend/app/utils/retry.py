from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[BaseException, int, float], None] | None = None,
) -> T:
    """Call func with exponential backoff. Re-raises the last error."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except exceptions as exc:
            if attempt >= attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if on_retry is not None:
                on_retry(exc, attempt, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover
