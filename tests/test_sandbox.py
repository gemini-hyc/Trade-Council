from pathlib import Path

from trade_council.extensions.sandbox import run_compute_method


def test_compute_method_receives_json_context(tmp_path: Path) -> None:
    script = tmp_path / "compute.py"
    script.write_text(
        "import json, sys\n"
        "ctx = json.load(sys.stdin)\n"
        "print(json.dumps({'signal': ctx['signal'], 'ok': True}))\n",
        encoding="utf-8",
    )
    result = run_compute_method(script, {"signal": "positive"})
    assert result == {"signal": "positive", "ok": True}

