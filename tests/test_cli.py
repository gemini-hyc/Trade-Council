from pathlib import Path

from trade_council.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[1]


def test_version_command(capsys) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_config_validate_command(capsys) -> None:
    assert main(["config", "validate", "--root", str(ROOT)]) == 0
    output = capsys.readouterr().out
    assert '"ok": true' in output
    assert '"methods": 1' in output


def test_methods_list_command(capsys) -> None:
    assert main(["methods", "list", "--root", str(ROOT)]) == 0
    assert "example-method\tenabled" in capsys.readouterr().out


def test_analyze_accepts_trade_context_flags() -> None:
    args = build_parser().parse_args(
        [
            "analyze",
            "600519.SH",
            "--capital",
            "100000",
            "--position-state",
            "holding",
            "--position-shares",
            "500",
            "--position-opened-date",
            "2026-10-20",
            "--board",
            "main",
            "--allow-web",
        ]
    )
    assert args.capital == 100_000
    assert args.position_state == "holding"
    assert args.position_shares == 500
    assert args.position_opened_date == "2026-10-20"
    assert args.board == "main"
    assert args.allow_web is True


def test_operations_commands_are_available() -> None:
    parser = build_parser()
    runs = parser.parse_args(["runs", "list", "--json"])
    assert runs.runs_command == "list"
    assert runs.json is True
    cleanup = parser.parse_args(["runs", "cleanup"])
    assert cleanup.apply is False
    export = parser.parse_args(["export", "--run-id", "example"])
    assert export.run_id == "example"
