from pathlib import Path

import pytest
from pydantic import ValidationError

from trade_council.config import load_project_config
from trade_council.errors import MethodReferenceError
from trade_council.extensions.loader import discover_methods, resolve_methods
from trade_council.schemas import MethodSelection

ROOT = Path(__file__).resolve().parents[1]


def test_example_method_is_discovered() -> None:
    registry = discover_methods(ROOT / "methods")
    method = registry.get("example-method")
    assert method.manifest.domain == "technical"
    assert method.manifest.type == "prompt"
    assert method.manifest.requires.datasets == []


def test_team_override_enables_method() -> None:
    config = load_project_config(ROOT)
    registry = discover_methods(ROOT / "methods")
    resolved = resolve_methods(registry, config.team)
    by_id = {item.manifest.id: item for item in resolved}
    assert by_id["example-method"].enabled is True


def test_unknown_team_method_is_rejected() -> None:
    config = load_project_config(ROOT)
    registry = discover_methods(ROOT / "methods")
    config.team.methods["missing-method"] = MethodSelection(enabled=True)
    with pytest.raises(MethodReferenceError, match="missing-method"):
        resolve_methods(registry, config.team)


def test_manifest_requires_are_typed() -> None:
    with pytest.raises(ValidationError):
        MethodSelection.model_validate({"enabled": "yes"})
