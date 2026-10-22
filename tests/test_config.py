from pathlib import Path

import pytest

from trade_council.config import load_project_config
from trade_council.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_project_config_loads() -> None:
    config = load_project_config(ROOT)
    assert config.app.name == "Trade-Council"
    assert config.app.default_profile == "batch"
    assert config.data.news_default_enabled is False
    assert config.data.quant_db_enabled is False
    assert set(config.models.profiles) == {"batch", "deep"}
    assert len(config.team.roles) == 12


def test_invalid_app_config_raises(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "app.yaml").write_text("name: Trade-Council\n", encoding="utf-8")
    (configs / "team.yaml").write_text(
        "lead:\n  id: lead\n  type: code_orchestrator\nroles: []\nmethods: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Invalid app config"):
        load_project_config(tmp_path)
