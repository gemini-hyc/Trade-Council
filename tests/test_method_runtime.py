from __future__ import annotations

from pathlib import Path

from trade_council.engineering.evidence import EvidenceIndex
from trade_council.extensions.loader import ResolvedMethod
from trade_council.extensions.runtime import (
    build_role_method_context,
    execute_method,
    validate_schema_subset,
)
from trade_council.schemas import AnalysisMethodManifest


def _evidence() -> EvidenceIndex:
    index = EvidenceIndex("snap-method")
    index.add(
        dataset_id="market.daily",
        path="market/daily",
        primary_key="600519.SH",
        event_date="2026-10-16",
        available_at="2026-10-16 15:00:00",
        field="close",
        value=100.0,
    )
    return index


def _code_method(
    root: Path,
    *,
    method_id: str,
    enabled: bool = True,
    required_field: str = "close",
    script: str | None = None,
) -> ResolvedMethod:
    path = root / method_id / "manifest.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""
id: {method_id}
name: {method_id}
version: 1.2.3
domain: technical
type: code
enabled: {str(enabled).lower()}
requires:
  datasets: [market.daily]
  fields: [{required_field}]
outputs:
  schema: schema.json
  evidence_policy: required
parameters: {{}}
""",
        encoding="utf-8",
    )
    (path.parent / "method.md").write_text(f"# {method_id}\n", encoding="utf-8")
    (path.parent / "compute.py").write_text(
        script
        or (
            "import json, sys\n"
            "ctx = json.load(sys.stdin)\n"
            "eid = ctx['evidence'][0]['evidence_id']\n"
            "print(json.dumps({'signal': 'positive', 'summary': 'ok', "
            "'evidence_ids': [eid]}))\n"
        ),
        encoding="utf-8",
    )
    (path.parent / "schema.json").write_text(
        """
{
  "type": "object",
  "additionalProperties": false,
  "required": ["signal", "summary", "evidence_ids"],
  "properties": {
    "signal": {"type": "string", "enum": ["positive", "neutral", "negative", "insufficient"]},
    "summary": {"type": "string", "minLength": 1},
    "evidence_ids": {"type": "array", "items": {"type": "string"}}
  }
}
""",
        encoding="utf-8",
    )
    manifest = AnalysisMethodManifest(
        id=method_id,
        name=method_id,
        version="1.2.3",
        domain="technical",
        type="code",
        enabled=enabled,
        requires={"datasets": ["market.daily"], "fields": [required_field]},
        outputs={"schema": "schema.json", "evidence_policy": "required"},
    )
    return ResolvedMethod(manifest=manifest, path=path, enabled=enabled)


def test_code_method_runs_and_normalizes_result(tmp_path: Path) -> None:
    method = _code_method(tmp_path, method_id="volume-breakout")
    result = execute_method(
        method,
        data_pack_dir=tmp_path,
        evidence=_evidence(),
        manifest={"ts_code": "600519.SH", "asof": "2026-10-16", "snapshot_id": "snap-method"},
    )
    assert result["status"] == "computed"
    assert result["result"]["method_id"] == "volume-breakout"
    assert result["result"]["version"] == "1.2.3"
    assert result["result"]["structured_output"]["signal"] == "positive"


def test_method_requirements_are_checked_before_execution(tmp_path: Path) -> None:
    method = _code_method(tmp_path, method_id="missing-field", required_field="not_available")
    result = execute_method(
        method,
        data_pack_dir=tmp_path,
        evidence=_evidence(),
        manifest={"ts_code": "600519.SH", "asof": "2026-10-16"},
    )
    assert result["status"] == "skipped"
    assert result["result"] is None
    assert any("missing field not_available" in gap for gap in result["data_gaps"])


def test_disabled_method_is_isolated(tmp_path: Path) -> None:
    method = _code_method(tmp_path, method_id="disabled-method", enabled=False)
    result = execute_method(
        method,
        data_pack_dir=tmp_path,
        evidence=_evidence(),
        manifest={"ts_code": "600519.SH", "asof": "2026-10-16"},
    )
    assert result["status"] == "disabled"
    assert result["result"] is None


def test_invalid_method_output_does_not_become_a_result(tmp_path: Path) -> None:
    method = _code_method(
        tmp_path,
        method_id="bad-output",
        script="import json; print(json.dumps({'signal': 'invalid'}))\n",
    )
    result = execute_method(
        method,
        data_pack_dir=tmp_path,
        evidence=_evidence(),
        manifest={"ts_code": "600519.SH", "asof": "2026-10-16"},
    )
    assert result["status"] == "error"
    assert result["result"] is None
    assert any("missing required property summary" in error for error in result["errors"])


def test_multiple_methods_remain_separate_and_conflicts_are_visible(tmp_path: Path) -> None:
    first = _code_method(tmp_path, method_id="method-a")
    second = _code_method(tmp_path, method_id="method-b")
    executions = {
        method.manifest.id: execute_method(
            method,
            data_pack_dir=tmp_path,
            evidence=_evidence(),
            manifest={"ts_code": "600519.SH", "asof": "2026-10-16"},
        )
        for method in (first, second)
    }
    context = build_role_method_context(executions, ["method-a", "method-b"])
    assert [item["id"] for item in context["methods"]] == ["method-a", "method-b"]
    assert {item["result"]["method_id"] for item in context["methods"]} == {
        "method-a",
        "method-b",
    }
    assert context["conflict_groups"] == ["technical"]


def test_schema_subset_rejects_wrong_types() -> None:
    errors = validate_schema_subset(
        {"score": "high", "items": [1, "two"]},
        {
            "type": "object",
            "required": ["score", "items"],
            "properties": {
                "score": {"type": "number"},
                "items": {"type": "array", "items": {"type": "integer"}},
            },
        },
    )
    assert any("score: expected number" in error for error in errors)
    assert any("items[1]: expected integer" in error for error in errors)
