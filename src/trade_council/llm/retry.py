"""Bounded async retry for transport failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..errors import ProviderUnavailableError

T = TypeVar("T")


async def retry_call(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except ProviderUnavailableError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(base_delay * (2**attempt))
    assert last_error is not None
    raise last_error

