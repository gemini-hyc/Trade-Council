"""Small stdlib HTTP client used by model providers."""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.request
from typing import Any

from ..errors import InvalidResponseError, ProviderUnavailableError


def _request_json_sync(
    url: str,
    *,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ProviderUnavailableError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as exc:
        raise ProviderUnavailableError(f"Cannot reach {url}: {exc}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidResponseError(f"Provider returned non-JSON response: {body[:200]}") from exc
    if not isinstance(parsed, dict):
        raise InvalidResponseError("Provider response root must be an object")
    return parsed


async def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _request_json_sync,
        url,
        payload=payload,
        headers=headers,
        timeout=timeout,
    )
