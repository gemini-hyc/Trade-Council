"""Prompt loading and content hashing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError


@dataclass(frozen=True)
class PromptDocument:
    name: str
    text: str
    sha256: str
    path: Path


def load_prompt(path: Path, name: str | None = None) -> PromptDocument:
    if not path.is_file():
        raise ConfigError(f"Prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    return PromptDocument(
        name=name or path.stem,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        path=path,
    )


class PromptRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, name: str) -> PromptDocument:
        return load_prompt(self.root / f"{name}.md", name=name)

