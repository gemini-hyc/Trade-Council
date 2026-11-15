import json
from dataclasses import replace
from pathlib import Path

import pytest

from trade_council import cli
from trade_council.config import load_project_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    config = load_project_config(ROOT)
    config = replace(config, app=config.app.model_copy(update={"run_dir": str(tmp_path / "runs")}))
    monkeypatch.setattr(cli, "load_project_config", lambda _: config)

    def forbidden(*args, **kwargs):
        raise AssertionError("Current-agent commands must not instantiate an API client/provider/workflow")

    for name in ["LLMClient", "WorkflowEngine", "build_provider"]:
        monkeypatch.setattr(cli, name, forbidden)

    def build(code, asof, out, *args, **kwargs):
        out.mkdir(parents=True, exist_ok=True)
        manifest = {"ts_code": "600362.SH", "stock_name": "江西铜业", "asof": "2026-11-13",
                    "snapshot_id": "test-snapshot"}
        (out / "manifest.json").write_text(json.dumps(manifest))
        (out / "DATA_DIGEST.md").write_text("Synthetic input")
        (out / "evidence.json").write_text('{"items":[]}')
        return manifest

    monkeypatch.setattr(cli, "build_datapack", build)
    return config, tmp_path


def test_default_analysis_prepares_current_agent_without_api(isolated, capsys):
    config, tmp = isolated
    args = ["analyze", "600362.SH", "--run-id", "current", "--datapack", str(tmp / "pack")]
    assert cli.main(args) == 0
    run = Path(config.app.run_dir) / "current"
    state = json.loads((run / "run.json").read_text())
    assert state["status"] == "AWAITING_CURRENT_AGENT"
    assert state["profile"] == "deep"
    assert state["external_model_calls"] == 0
    assert state["trade_context"]["capital"] is None
    request = json.loads((run / "current_agent_request.json").read_text())
    assert len(request["roles"]) == 11
    assert all(r["prompt_sha256"] and r["schema"] for r in request["roles"])
    assert "DeepSeek" in (run / "CURRENT_AGENT_TASK.md").read_text()
    assert not (run / "decision.json").exists()
    assert not (run / "model_calls.jsonl").exists()
    assert "AWAITING_CURRENT_AGENT" in capsys.readouterr().out
    assert cli.main(["report", "--run-id", "current", "--datapack", str(tmp / "pack")]) == 2

    # A repeat/resume must preserve actual work, not reset it to a fresh pending task.
    state["status"] = "DECIDED"
    (run / "run.json").write_text(json.dumps(state))
    assert cli.main(args + ["--resume"]) == 0
    assert json.loads((run / "run.json").read_text())["status"] == "DECIDED"


def test_batch_and_quick_rejected_before_data_build(isolated, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Paused research must fail before reading/building data")

    monkeypatch.setattr(cli, "build_datapack", forbidden)
    assert cli.main(["analyze", "600362.SH", "--profile", "batch", "--allow-cloud"]) == 2
    assert cli.main(["analyze", "600362.SH", "--mode", "quick"]) == 2


def test_model_inspection_and_legacy_flags_never_make_api_call(isolated, capsys):
    for command in [["models", "list"], ["models", "check"],
                    ["models", "route", "--profile", "deep", "--role", "trader"]]:
        assert cli.main(command) == 0
    assert '"external_api": false' in capsys.readouterr().out
    assert cli.main(["models", "probe", "--profile", "deep", "--role", "trader", "--allow-cloud"]) == 2
    assert cli.main(["models", "route", "--profile", "batch", "--role", "trader"]) == 2


def test_news_flag_and_snapshot_conflict(isolated):
    config, tmp = isolated
    args = ["analyze", "600362.SH", "--run-id", "news", "--datapack", str(tmp / "pack"),
            "--include-news", "--allow-cloud", "--allow-web"]
    assert cli.main(args) == 0
    run = Path(config.app.run_dir) / "news"
    request = json.loads((run / "current_agent_request.json").read_text())
    assert len(request["roles"]) == 12
    assert request["web_verification_requested"] is True
    manifest_path = tmp / "pack/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["snapshot_id"] = "different"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="snapshot"):
        cli.main(args + ["--resume"])
