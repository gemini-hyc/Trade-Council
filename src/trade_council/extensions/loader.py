"""Discover and resolve analysis method packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..config import ProjectConfig
from ..errors import MethodManifestError, MethodReferenceError
from ..schemas import AnalysisMethodManifest, TeamConfig
from .registry import LoadedMethod, MethodRegistry


@dataclass(frozen=True)
class ResolvedMethod:
    manifest: AnalysisMethodManifest
    path: Path
    enabled: bool


def _read_manifest(path: Path) -> AnalysisMethodManifest:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MethodManifestError(f"Cannot read method manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MethodManifestError(f"Method manifest must be a mapping: {path}")
    try:
        return AnalysisMethodManifest.model_validate(raw)
    except ValidationError as exc:
        raise MethodManifestError(f"Invalid method manifest {path}: {exc}") from exc


def discover_methods(methods_root: Path) -> MethodRegistry:
    """Discover all `manifest.yaml` files below the methods directory."""

    registry = MethodRegistry()
    if not methods_root.is_dir():
        raise MethodManifestError(f"Methods directory not found: {methods_root}")
    for manifest_path in sorted(methods_root.rglob("manifest.yaml")):
        registry.register(LoadedMethod(manifest=_read_manifest(manifest_path), path=manifest_path))
    return registry


def resolve_methods(registry: MethodRegistry, team: TeamConfig) -> list[ResolvedMethod]:
    """Resolve manifest defaults with team-level overrides."""

    discovered = registry.all()
    referenced = {
        method_id
        for role in team.roles
        for method_id in role.methods
    } | set(team.methods)
    missing = sorted(referenced - set(discovered))
    if missing:
        raise MethodReferenceError(f"Configured methods not found: {', '.join(missing)}")

    resolved: list[ResolvedMethod] = []
    for method_id, loaded in sorted(discovered.items()):
        selection = team.methods.get(method_id)
        enabled = selection.enabled if selection is not None else loaded.manifest.enabled
        resolved.append(ResolvedMethod(manifest=loaded.manifest, path=loaded.path, enabled=enabled))
    return resolved


def load_resolved_methods(config: ProjectConfig) -> list[ResolvedMethod]:
    registry = discover_methods(config.root / "methods")
    return resolve_methods(registry, config.team)

