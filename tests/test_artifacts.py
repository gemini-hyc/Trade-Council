import json
from pathlib import Path

from trade_council.artifacts import write_role_output


def test_role_json_and_markdown_are_separate(tmp_path: Path) -> None:
    json_path, md_path = write_role_output(
        tmp_path,
        phase="phase1",
        role_id="market-analyst",
        structured={"role_id": "market-analyst", "summary": "ok"},
        markdown="# Market Analyst\n",
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"] == "ok"
    assert json_path.read_text(encoding="utf-8").find("# Market Analyst") == -1
    assert md_path.read_text(encoding="utf-8") == "# Market Analyst\n"


def test_invalid_role_id_is_rejected(tmp_path: Path) -> None:
    try:
        write_role_output(
            tmp_path,
            phase="phase1",
            role_id="../escape",
            structured={},
            markdown="",
        )
    except ValueError as exc:
        assert "role_id" in str(exc)
    else:
        raise AssertionError("expected invalid role id to fail")

