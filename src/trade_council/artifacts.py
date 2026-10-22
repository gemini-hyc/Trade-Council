"""Helpers for keeping structured role output separate from prose."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_role_output(
    run_dir: Path,
    *,
    phase: str,
    role_id: str,
    structured: dict[str, Any],
    markdown: str,
) -> tuple[Path, Path]:
    """Write one role's JSON and Markdown artifacts without embedding prose in JSON."""

    if "/" in role_id or "\\" in role_id or role_id in {"", ".", ".."}:
        raise ValueError("role_id must be a simple file name")
    target = run_dir / "agents" / phase
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / f"{role_id}.json"
    md_path = target / f"{role_id}.md"
    json_path.write_text(
        json.dumps(structured, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path

