"""Persistent run state and role artifacts."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, cast

from ..artifacts import write_role_output


class RunStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.expanduser().resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "run.json"
        self._trace_lock = threading.Lock()

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        return cast(dict[str, Any], json.loads(self.state_path.read_text(encoding="utf-8")))

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def write_json(self, name: str, value: Any) -> Path:
        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_json(self, name: str) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads((self.run_dir / name).read_text(encoding="utf-8")))

    def role_json(self, phase: str, role_id: str) -> Path:
        return self.run_dir / "agents" / phase / f"{role_id}.json"

    def load_role(self, phase: str, role_id: str) -> dict[str, Any] | None:
        path = self.role_json(phase, role_id)
        if not path.is_file():
            return None
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def write_role(
        self,
        *,
        phase: str,
        role_id: str,
        structured: dict[str, Any],
        markdown: str,
    ) -> tuple[Path, Path]:
        return write_role_output(
            self.run_dir,
            phase=phase,
            role_id=role_id,
            structured=structured,
            markdown=markdown,
        )

    def write_trace(self, calls: list[dict[str, Any]]) -> None:
        path = self.run_dir / "model_calls.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for call in calls:
                handle.write(json.dumps(call, ensure_ascii=False) + "\n")

    def append_trace_call(self, call: dict[str, Any]) -> None:
        with self._trace_lock:
            path = self.run_dir / "model_calls.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(call, ensure_ascii=False) + "\n")
