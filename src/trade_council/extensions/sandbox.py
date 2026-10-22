"""Restricted subprocess execution for code-enabled methods."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..errors import MethodManifestError


def run_compute_method(
    compute_path: Path,
    context: dict[str, Any],
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Run compute.py with a JSON context and return its JSON stdout."""

    if not compute_path.is_file():
        raise MethodManifestError(f"Method compute file not found: {compute_path}")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONIOENCODING": "utf-8",
        "TRADE_COUNCIL_METHOD": "1",
    }
    try:
        completed = subprocess.run(
            [sys.executable, str(compute_path)],
            input=json.dumps(context, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=compute_path.parent,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MethodManifestError(f"Method timed out: {compute_path}") from exc
    if completed.returncode != 0:
        raise MethodManifestError(
            f"Method failed ({completed.returncode}): {completed.stderr[:300]}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MethodManifestError("Method stdout must be a JSON object") from exc
    if not isinstance(result, dict):
        raise MethodManifestError("Method result must be a JSON object")
    return result

