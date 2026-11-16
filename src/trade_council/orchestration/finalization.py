"""One completion path: validate the frozen run, then finalize it atomically.

The checker can establish structure, point-in-time integrity, evidence
references, and arithmetic. It cannot establish that the reasoning is correct,
and the code and docs must not imply otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from ..config import ProjectConfig
from ..engineering.financials import INCOME_FLOW_FIELDS, FinancialDataError, quarterize
from ..reporting.build import build_report
from ..schemas import DecisionPlan, FinalDecision, RoleOutput, TradeProposal
from .current_agent import verify_input_binding

ROLE_MODELS: dict[str, type[Any]] = {
    "research-manager": DecisionPlan,
    "trader": TradeProposal,
    "risk-manager": FinalDecision,
}
RESEARCH_ARTIFACTS = ("research_questions.json", "claims.json", "research_decision.json")
READY_STATUSES = {"AWAITING_CURRENT_AGENT", "DATA_READY", "DECIDED", "VALIDATED", "FINALIZED", "AUDITED"}


@dataclass
class Finding:
    code: str
    level: str
    message: str
    where: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "level": self.level, "message": self.message, "where": self.where}


@dataclass
class ValidationReport:
    run_id: str
    ok: bool = True
    findings: list[Finding] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    roles_checked: list[str] = field(default_factory=list)
    evidence_count: int = 0
    input_ok: bool = False

    def fail(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(code, "FAIL", message, where))
        self.ok = False

    def warn(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(code, "WARN", message, where))

    def note(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(code, "INFO", message, where))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "run_id": self.run_id,
            "ok": self.ok,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "roles_checked": self.roles_checked,
            "evidence_count": self.evidence_count,
            "input_binding_ok": self.input_ok,
            "findings": [finding.to_dict() for finding in self.findings],
            "todos": self.todos,
            "scope_note": (
                "本校验只覆盖结构、时点、证据引用与可计算关系，不代表研究结论正确，"
                "也不构成数据供应商完整性保证。"
            ),
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_evidence_universe(datapack: Path, run_dir: Path) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for path in (
        datapack / "evidence.json",
        run_dir / "supplemental_evidence.json",
        run_dir / "supplement" / "evidence.json",
    ):
        if not path.is_file():
            continue
        payload = _read_json(path)
        for item in payload.get("items", []):
            evidence_id = str(item.get("evidence_id"))
            items[evidence_id] = item
    return items


def _check_role_outputs(
    report: ValidationReport,
    request: dict[str, Any],
    run_dir: Path,
) -> list[str]:
    roles: list[str] = []
    for entry in request.get("roles", []):
        role_id = str(entry.get("role_id"))
        roles.append(role_id)
        relative = str(entry.get("output") or "")
        path = run_dir / relative
        if not relative or not path.is_file():
            report.fail("ROLE_MISSING", f"缺少启用角色的产物：{role_id}", relative)
            report.todos.append(f"补齐 {role_id} 的输出（{relative}）后重新 validate")
            continue
        try:
            payload = _read_json(path)
        except json.JSONDecodeError as exc:
            report.fail("ROLE_JSON_INVALID", f"{role_id} 的 JSON 无法解析：{exc}", relative)
            continue
        model = ROLE_MODELS.get(role_id, RoleOutput)
        try:
            model.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - pydantic error text is the useful output
            report.fail("ROLE_SCHEMA_INVALID", f"{role_id} 不符合 Schema：{exc}", relative)
            report.todos.append(f"按 current_agent_request.json 中的 Schema 修正 {role_id}")
            continue
        if role_id not in ROLE_MODELS:
            unknown = sorted(set(payload) - set(model.model_fields))
            if unknown:
                report.fail(
                    "ROLE_UNKNOWN_FIELD",
                    f"{role_id} 含未定义字段：{', '.join(unknown)}",
                    relative,
                )
                report.todos.append(f"删除或改用 Schema 中的字段：{', '.join(unknown)}")
        report.roles_checked.append(role_id)
    return roles


def _collect_evidence_ids(
    request_roles: list[str],
    run_dir: Path,
    decision: dict[str, Any],
    research: dict[str, Any],
    claims: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []

    def add(ids: Any, where: str) -> None:
        for value in ids or []:
            references.append((str(value), where))

    for entry in (run_dir / "agents").glob("phase*/*.json") if (run_dir / "agents").is_dir() else []:
        payload = _read_json(entry)
        if isinstance(payload, dict):
            for finding in payload.get("key_findings", []) or []:
                add(finding.get("evidence_ids"), str(entry.name))
            add(payload.get("evidence_ids"), str(entry.name))
    add(decision.get("evidence_ids"), "decision.json")
    add(research.get("evidence_ids"), "research_decision.json")
    for claim in claims:
        add(claim.get("supporting_evidence_ids"), "claims.json")
        add(claim.get("counter_evidence_ids"), "claims.json")
    return references


def _verify_financials(report: ValidationReport, datapack: Path) -> None:
    bridge_path = datapack / "earnings_bridge.json"
    if not bridge_path.is_file():
        report.warn("FINANCIAL_ARTIFACT_MISSING", "缺少 earnings_bridge.json，无法复核季度财务", str(datapack))
        return
    income_path = datapack / "income.csv"
    if not income_path.is_file():
        report.warn("FINANCIAL_SOURCE_MISSING", "缺少 income.csv，无法从原表重算单季", str(datapack))
        return
    bridge = _read_json(bridge_path)
    income = pd.read_csv(income_path)
    if "available_at" not in income.columns and "ann_date" in income.columns:
        income["available_at"] = pd.to_datetime(income["ann_date"], errors="coerce")
    try:
        series = quarterize(income, symbol=str(bridge.get("symbol") or ""), flow_fields=INCOME_FLOW_FIELDS)
    except FinancialDataError as exc:
        report.fail("FINANCIAL_RECOMPUTE_FAILED", f"季度重算失败：{exc}", str(income_path))
        return
    mismatches = 0
    for quarter in bridge.get("quarters", []):
        record = series.by_end_date(str(quarter.get("end_date")))
        if record is None or record.status != "ok":
            continue
        for line in quarter.get("lines", []):
            line_id = str(line.get("line_id"))
            if line_id not in INCOME_FLOW_FIELDS:
                continue
            cell = record.values.get(line_id)
            expected = line.get("amount")
            actual = cell.single if cell is not None else None
            if expected is None or actual is None:
                continue
            if abs(float(expected) - float(actual)) > 1.0:
                mismatches += 1
                report.fail(
                    "FINANCIAL_MISMATCH",
                    f"{quarter.get('end_date')} {line_id} 与从原表重算的单季值不一致",
                    str(bridge_path),
                )
    for check in bridge.get("checks", []):
        if check.get("status") == "fail":
            report.fail(
                "FINANCIAL_CHECK_FAIL",
                f"{check.get('end_date', '')} 桥接对账失败：{check.get('label')} "
                f"差额 {check.get('difference')}",
                str(bridge_path),
            )
    if mismatches == 0 and not any(
        finding.code.startswith("FINANCIAL") and finding.level == "FAIL" for finding in report.findings
    ):
        report.note("FINANCIAL_RECOMPUTED", "季度单季值与原表重算一致（容差 1 元）", str(bridge_path))


def _validate_research(
    report: ValidationReport,
    run_dir: Path,
    decision: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    research: dict[str, Any] = {}
    claims: list[dict[str, Any]] = []
    questions_path = run_dir / "research_questions.json"
    claims_path = run_dir / "claims.json"
    research_path = run_dir / "research_decision.json"

    if questions_path.is_file():
        questions = _read_json(questions_path)
        items = questions.get("questions", questions) if isinstance(questions, dict) else questions
        if not isinstance(items, list):
            report.fail("RESEARCH_SCHEMA_INVALID", "research_questions.json 必须是问题数组", str(questions_path))
        else:
            if len(items) > 5:
                report.warn("RESEARCH_TOO_MANY_QUESTIONS", "关键问题超过 5 个，容易失去焦点", str(questions_path))
            for item in items:
                status = str(item.get("status", ""))
                if status not in {"open", "answered", "unresolved"}:
                    report.fail(
                        "RESEARCH_SCHEMA_INVALID",
                        f"研究问题状态非法：{item.get('question_id')} -> {status}",
                        str(questions_path),
                    )
                if status == "unresolved" and str(item.get("priority")) == "critical":
                    report.warn(
                        "CRITICAL_QUESTION_UNRESOLVED",
                        f"关键问题仍未决：{item.get('question_id')}；相关结论需限定范围",
                        str(questions_path),
                    )
    else:
        report.warn("RESEARCH_QUESTIONS_MISSING", "缺少 research_questions.json", str(run_dir))

    if claims_path.is_file():
        payload = _read_json(claims_path)
        claims = payload.get("claims", payload) if isinstance(payload, dict) else payload
        if not isinstance(claims, list):
            report.fail("RESEARCH_SCHEMA_INVALID", "claims.json 必须是论点数组", str(claims_path))
            claims = []
        for claim in claims:
            if str(claim.get("kind") or "inference") not in {"fact", "assumption", "inference"}:
                report.fail(
                    "RESEARCH_SCHEMA_INVALID",
                    f"论点类型非法：{claim.get('claim_id')}",
                    str(claims_path),
                )
            if str(claim.get("status") or "intact") not in {
                "intact",
                "weakened",
                "retracted",
                "unresolved",
            }:
                report.fail(
                    "RESEARCH_SCHEMA_INVALID",
                    f"论点状态非法：{claim.get('claim_id')}",
                    str(claims_path),
                )
    else:
        report.warn("RESEARCH_CLAIMS_MISSING", "缺少 claims.json", str(run_dir))

    if research_path.is_file():
        research = _read_json(research_path)
        try:
            from ..research_schemas import ResearchDecision

            parsed = ResearchDecision.model_validate(research)
        except Exception as exc:  # noqa: BLE001
            report.fail("RESEARCH_DECISION_INVALID", f"research_decision.json 不符合 Schema：{exc}", str(research_path))
            return research, claims
        retracted = {
            str(claim.get("claim_id"))
            for claim in claims
            if str(claim.get("status")) == "retracted"
        }
        used = set(parsed.key_claim_ids) & retracted
        if used:
            report.fail(
                "RETRACTED_CLAIM_IN_USE",
                f"结论仍引用已撤回论点：{', '.join(sorted(used))}",
                str(research_path),
            )
            report.todos.append("撤回论点后必须重算依赖它的估值与执行条件，再更新 research_decision")
        known = {str(claim.get("claim_id")) for claim in claims}
        unknown = [claim for claim in parsed.key_claim_ids if claim not in known]
        if unknown:
            report.fail(
                "RESEARCH_CLAIM_UNKNOWN",
                f"结论引用了不存在的论点：{', '.join(unknown)}",
                str(research_path),
            )
        execution = parsed.execution
        if execution.status == "actionable" and not execution.target_account_weight and not execution.sell_fraction_of_current:
            report.fail(
                "EXECUTION_WITHOUT_SIZE",
                "execution_status=actionable 但没有可执行的仓位或减仓比例",
                str(research_path),
            )
        if execution.status == "actionable" and not decision.get("action"):
            report.fail("EXECUTION_WITHOUT_DECISION", "缺少 decision.json 支撑可执行状态", str(run_dir))
    else:
        report.warn("RESEARCH_DECISION_MISSING", "缺少 research_decision.json", str(run_dir))
    return research, claims


def validate_run(config: ProjectConfig, run_id: str) -> ValidationReport:
    """Check a run without calling a model and without marking it complete."""

    run_dir = config.root / config.app.run_dir / run_id
    report = ValidationReport(run_id=run_id)
    state_path = run_dir / "run.json"
    if not state_path.is_file():
        report.fail("RUN_MISSING", f"运行不存在：{run_dir}")
        return report
    run = _read_json(state_path)
    status = str(run.get("status") or "")
    if status not in READY_STATUSES:
        report.fail("RUN_STATUS_UNKNOWN", f"未知运行状态：{status}", str(state_path))
    if status in {"AWAITING_CURRENT_AGENT", "DATA_READY"}:
        report.fail(
            "ANALYSIS_NOT_READY",
            "当前只有准备或数据就绪状态，不能校验为完成",
            str(state_path),
        )
        report.todos.append("先完成启用角色的分析与 decision.json，再运行 validate")
        return report

    problems = verify_input_binding(run, config.root)
    if problems:
        for problem in problems:
            report.fail("INPUT_TAMPERED", problem, str(run.get("data_pack_dir")))
        report.todos.append("输入已变化：用相同输入重跑，或新建 run 并重新分析；不得沿用旧结论")
    else:
        report.input_ok = True

    request_path = run_dir / "current_agent_request.json"
    if not request_path.is_file():
        report.fail("REQUEST_MISSING", "缺少 current_agent_request.json，无法确定应有角色", str(run_dir))
        return report
    request = _read_json(request_path)
    _check_role_outputs(report, request, run_dir)

    datapack = Path(str(run.get("data_pack_dir") or ""))
    decision_path = run_dir / "decision.json"
    decision: dict[str, Any] = {}
    if not decision_path.is_file():
        report.fail("DECISION_MISSING", "缺少 decision.json", str(run_dir))
        report.todos.append("按 FinalDecision Schema 保存 decision.json")
    else:
        decision = _read_json(decision_path)
        try:
            FinalDecision.model_validate(decision)
        except Exception as exc:  # noqa: BLE001
            report.fail("DECISION_SCHEMA_INVALID", f"decision.json 不符合 Schema：{exc}", str(decision_path))

    research, claims = _validate_research(report, run_dir, decision)

    evidence = load_evidence_universe(datapack, run_dir)
    report.evidence_count = len(evidence)
    asof = str(run.get("asof") or "")[:10]
    for evidence_id, where in _collect_evidence_ids(
        report.roles_checked, run_dir, decision, research, claims
    ):
        item = evidence.get(evidence_id)
        if item is None:
            report.fail("EVIDENCE_UNKNOWN", f"引用了不存在的证据：{evidence_id}", where)
            continue
        available_at = str(item.get("available_at") or "")[:10]
        if asof and available_at and available_at > asof:
            report.fail(
                "EVIDENCE_FUTURE",
                f"证据 {evidence_id} 在 {available_at} 才可得，晚于基准日 {asof}",
                where,
            )
        value_type = str(item.get("value_type") or "")
        if value_type and value_type != "number" and isinstance(item.get("value"), (int, float)):
            report.fail(
                "EVIDENCE_TYPE_MISMATCH",
                f"证据 {evidence_id} 标为 {value_type} 但值是数字",
                where,
            )

    if datapack.is_dir():
        _verify_financials(report, datapack)
    else:
        report.fail("DATAPACK_MISSING", f"数据包目录不存在：{datapack}", str(run_dir))

    if not report.ok and not report.todos:
        report.todos.append("按失败项逐条修复后重新运行 validate")
    payload = report.to_dict()
    (run_dir / "validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run["validation"] = {
        "ok": report.ok,
        "fail": sum(1 for finding in report.findings if finding.level == "FAIL"),
        "warn": sum(1 for finding in report.findings if finding.level == "WARN"),
        "checked_at": payload["checked_at"],
    }
    if run.get("status") not in {"FINALIZED"}:
        run["status"] = "VALIDATED" if report.ok else "VALIDATION_FAILED"
    run["updated_at"] = payload["checked_at"]
    state_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def finalize_run(config: ProjectConfig, run_id: str) -> dict[str, Any]:
    """Validate, run deterministic risk checks, build the report, save atomically."""

    run_dir = config.root / config.app.run_dir / run_id
    report = validate_run(config, run_id)
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "ok": False,
        "stage": "validation",
        "validation_ok": report.ok,
        "failures": [finding.to_dict() for finding in report.findings if finding.level == "FAIL"],
        "todos": report.todos,
    }
    if not report.ok:
        record["reason"] = "校验未通过，未生成正式报告"
        (run_dir / "finalize.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _set_status(run_dir, "VALIDATION_FAILED")
        return record

    run = _read_json(run_dir / "run.json")
    datapack = Path(str(run.get("data_pack_dir") or ""))
    audit = build_report(run_dir, datapack)
    record["audit_verdict"] = audit["verdict"]
    record["audit_issues"] = audit["issues"]
    if audit["verdict"] == "FAIL":
        record["stage"] = "audit"
        record["reason"] = "报告审计未通过，只保留诊断草稿，未交付正式报告"
        (run_dir / "finalize.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _set_status(run_dir, "AUDIT_FAILED")
        return record

    from ..data.snapshot import sha256_file

    report_html = run_dir / "report.html"
    report_md = run_dir / "report.md"
    record.update(
        {
            "ok": True,
            "stage": "finalized",
            "report_html_sha256": sha256_file(report_html),
            "report_md_sha256": sha256_file(report_md),
            "finalized_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    (run_dir / "finalize.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _set_status(run_dir, "FINALIZED")
    return record


def _set_status(run_dir: Path, status: str) -> None:
    state_path = run_dir / "run.json"
    run = _read_json(state_path)
    run["status"] = status
    run["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    state_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")


def load_finalization(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "finalize.json"
    if not path.is_file():
        return None
    return cast(dict[str, Any], _read_json(path))
