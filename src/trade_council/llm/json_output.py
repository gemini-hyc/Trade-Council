"""Parse model JSON output with a bounded repair path."""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import InvalidResponseError
from .base import Message


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise InvalidResponseError("Model response does not contain a JSON object")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidResponseError(f"Invalid JSON from model: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidResponseError("Model JSON output must be an object")
    return parsed


def repair_messages(original_content: str) -> list[Message]:
    return [
        Message(
            role="system",
            content="Return only valid JSON. Do not add markdown or commentary.",
        ),
        Message(
            role="user",
            content=f"Repair this model output into one valid JSON object:\n{original_content}",
        ),
    ]

