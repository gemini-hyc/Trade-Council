"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import ConfigError
from .llm.config import load_models_config
from .llm.models import ModelsConfig
from .schemas import AppConfig, DataConfig, TeamConfig


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    app: AppConfig
    data: DataConfig
    models: ModelsConfig
    team: TeamConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


def load_app_config(path: Path) -> AppConfig:
    try:
        return AppConfig.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid app config {path}: {exc}") from exc


def load_team_config(path: Path) -> TeamConfig:
    try:
        return TeamConfig.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid team config {path}: {exc}") from exc


def load_data_config(path: Path) -> DataConfig:
    try:
        return DataConfig.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid data config {path}: {exc}") from exc


def load_project_config(root: Path) -> ProjectConfig:
    root = root.expanduser().resolve()
    app = load_app_config(root / "configs" / "app.yaml")
    data = load_data_config(root / "configs" / "data.yaml")
    models = load_models_config(root / "configs" / "models.yaml")
    team = load_team_config(root / "configs" / "team.yaml")
    return ProjectConfig(root=root, app=app, data=data, models=models, team=team)
