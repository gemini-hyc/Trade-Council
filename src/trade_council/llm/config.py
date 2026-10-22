"""Load and validate model configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from .models import ModelsConfig


def load_models_config(path: Path) -> ModelsConfig:
    if not path.is_file():
        raise ConfigError(f"Model config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ModelsConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid model config {path}: {exc}") from exc

