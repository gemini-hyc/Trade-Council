"""In-memory registry for discovered methods."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import MethodManifestError
from ..schemas import AnalysisMethodManifest


@dataclass(frozen=True)
class LoadedMethod:
    manifest: AnalysisMethodManifest
    path: Path


class MethodRegistry:
    """Registry keyed by stable method id."""

    def __init__(self) -> None:
        self._methods: dict[str, LoadedMethod] = {}

    def register(self, method: LoadedMethod) -> None:
        method_id = method.manifest.id
        if method_id in self._methods:
            raise MethodManifestError(f"Duplicate method id: {method_id}")
        self._methods[method_id] = method

    def get(self, method_id: str) -> LoadedMethod:
        try:
            return self._methods[method_id]
        except KeyError as exc:
            raise MethodManifestError(f"Unknown method id: {method_id}") from exc

    def all(self) -> dict[str, LoadedMethod]:
        return dict(self._methods)

