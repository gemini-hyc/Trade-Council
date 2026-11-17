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
from ..data.snapshot import sha256_file
from ..dates import is_after, to_iso_date
from ..decision.risk_rules import (
    TradeContext,
    load_risk_policy,
    tradability_status,
)
from ..decision.rule_tree import load_rule_tree, validate_rule_tree
from ..engineering.financials import INCOME_FLOW_FIELDS, FinancialDataError, quarterize
from ..reporting.build import build_report
from ..research.gaps import GAP_LABELS, GapRecord
from ..research.propagation import (
    load_revisions,
    pending_reviews,
    pending_role_reviews,
    reconcile_revisions,
    role_claim_refs,
    role_payloads,
)
from ..research_schemas import ClaimSet, QuestionSet, ResearchDecision
from ..schemas import DecisionPlan, FinalDecision, RoleOutput, TradeProposal
from .current_agent import verify_input_binding

ROLE_MODELS: dict[str, type[Any]] = {
    "research-manager": DecisionPlan,
    "trader": TradeProposal,
    "risk-manager": FinalDecision,
}
RESEARCH_ARTIFACTS = ("research_questions.json", "claims.json", "research_decision.json")
GAP_FILE = "data_gaps.json"
RULE_FILE = "trading_rules.json"
SEAL_SCHEMA_VERSION = "seal-1"
READY_STATUSES = {
    "AWAITING_CURRENT_AGENT",
    "DATA_READY",
    "DECIDED",
    "VALIDATED",
    "VALIDATION_FAILED",
    "FINALIZED",
    "AUDITED",
    "AUDIT_FAILED",
}
PREPARED_STATUSES = {"AWAITING_CURRENT_AGENT", "DATA_READY"}

#: Every file whose content can change the conclusion, bound into the seal.
SEAL_FILES = (
    "decision.json",
    "research_questions.json",
    "claims.json",
    "research_decision.json",
    "data_gaps.json",
    "trading_rules.json",
    "risk_scorecard.json",
    "supplemental_evidence.json",
    "revisions.jsonl",
    "evidence_imports.jsonl",
    "report.html",
    "report.md",
    "report_view.json",
)
SEAL_RUN_FIELDS = (
    "input_binding",
    "trade_context",
    "capability_flags",
    "financial_override",
    "symbol",
    "asof",
)


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
    capabilities: dict[str, bool] = field(default_factory=dict)

    def fail(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(code, "FAIL", message, where))
        self.ok = False

    def warn(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(code, "WARN", message, where))

    def note(self, code: str, message: str, where: str = "") -> None:
        self.findings.append(Finding(code, "INFO", message, where))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.1",
            "run_id": self.run_id,
            "ok": self.ok,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "roles_checked": self.roles_checked,
            "evidence_count": self.evidence_count,
            "input_binding_ok": self.input_ok,
            "capabilities": self.capabilities,
            "findings": [finding.to_dict() for finding in self.findings],
            "todos": self.todos,
            "scope_note": (
                "本校验只覆盖结构、时点、证据引用与可计算关系，不代表研究结论正确，"
                "也不构成数据供应商完整性保证。"
            ),
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_sha256(payload: Any) -> str:
    import hashlib

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _analysis_artifacts(run_dir: Path, request: dict[str, Any]) -> list[str]:
    """Required analysis products, in the order a reader would notice them."""

    missing: list[str] = []
    for entry in request.get("roles", []):
        relative = str(entry.get("output") or "")
        if not relative or not (run_dir / relative).is_file():
            missing.append(relative or f"<{entry.get('role_id')}>")
    for name in ("decision.json", *RESEARCH_ARTIFACTS):
        if not (run_dir / name).is_file():
            missing.append(name)
    return missing


def build_seal(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Bind a finalized report to every input that could change its meaning.

    The digest covers the frozen data pack, the role outputs, the research
    files, the revision log, the rule tree and the account context. A later
    edit to any of them invalidates the seal, so an old report cannot be
    exported as if it were the current conclusion.
    """

    dependencies: dict[str, str] = {}
    for name in SEAL_FILES:
        path = run_dir / name
        if path.is_file():
            dependencies[name] = sha256_file(path)
    agents = run_dir / "agents"
    if agents.is_dir():
        for path in sorted(agents.glob("phase*/*.json")):
            dependencies[str(path.relative_to(run_dir))] = sha256_file(path)
    recompute = run_dir / "recompute"
    if recompute.is_dir():
        for path in sorted(recompute.rglob("*")):
            if path.is_file():
                dependencies[str(path.relative_to(run_dir))] = sha256_file(path)
    recompute = run_dir / "recompute"
    if recompute.is_dir():
        for path in sorted(recompute.rglob("*")):
            if path.is_file():
                dependencies[str(path.relative_to(run_dir))] = sha256_file(path)
    for field_name in SEAL_RUN_FIELDS:
        dependencies[f"run.json#{field_name}"] = _canonical_sha256(run.get(field_name))
    binding = run.get("input_binding") or {}
    dependencies["datapack#content_hash"] = str(binding.get("content_hash") or "")
    dependencies["datapack#manifest_sha256"] = str(binding.get("manifest_sha256") or "")
    for name, value in sorted((binding.get("rule_fingerprints") or {}).items()):
        dependencies[f"rule#{name}"] = str(value)
    return {
        "schema_version": SEAL_SCHEMA_VERSION,
        "digest": _canonical_sha256(dependencies),
        "dependencies": dependencies,
    }


def verify_seal(config: ProjectConfig, run_id: str) -> list[str]:
    """Return every dependency that changed after the report was sealed."""

    run_dir = config.root / config.app.run_dir / run_id
    record = load_finalization(run_dir)
    if not record or not record.get("ok"):
        return ["运行尚未成功封存，没有可比对的封存记录"]
    seal = record.get("seal") or {}
    if not seal.get("digest"):
        return ["封存记录缺少 seal.digest（旧版封存），无法证明结论仍对应当前输入"]
    run_path = run_dir / "run.json"
    if not run_path.is_file():
        return ["run.json 缺失，无法核对封存依赖"]
    run = _read_json(run_path)
    problems = verify_input_binding(run, config.root)
    current = build_seal(run_dir, run)
    recorded = seal.get("dependencies") or {}
    for name, digest in sorted(current["dependencies"].items()):
        previous = recorded.get(name)
        if previous is None:
            problems.append(f"封存后新增了依赖：{name}")
        elif previous != digest:
            problems.append(f"封存后内容已变化：{name}")
    for name in sorted(set(recorded) - set(current["dependencies"])):
        problems.append(f"封存时的依赖已不存在：{name}")
    if not problems and current["digest"] != seal.get("digest"):
        problems.append("封存摘要不一致")
    return problems


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


def _verify_financials(report: ValidationReport, datapack: Path, run: dict[str, Any]) -> None:
    from ..research.recompute import finance_dir

    finance = finance_dir(run, datapack)
    bridge_path = finance / "earnings_bridge.json"
    if not bridge_path.is_file():
        report.warn("FINANCIAL_ARTIFACT_MISSING", "缺少 earnings_bridge.json，无法复核季度财务", str(finance))
        return
    if finance != datapack:
        report.note(
            "FINANCIAL_RECOMPUTED_AT_RUN",
            f"财务数字来自运行级重算情景 {finance.name}（冻结数据包未改动）",
            str(finance),
        )
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
    run: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The three research files are mandatory for a current-agent run.

    Presence is checked first, then the strict schemas, then the links between
    them (run id, revision id, duplicate ids, dependencies, unknown fields).
    A run recorded without ``analysis_schema`` is read through an explicit
    legacy path: missing files stay WARN, but anything present is still checked
    against the same schema, so a legacy run cannot smuggle in a typo.
    """

    research: dict[str, Any] = {}
    claims: list[dict[str, Any]] = []
    run_id = str(run.get("run_id") or run_dir.name)
    legacy = not run.get("analysis_schema")
    questions_path = run_dir / "research_questions.json"
    claims_path = run_dir / "claims.json"
    research_path = run_dir / "research_decision.json"

    for name in RESEARCH_ARTIFACTS:
        if (run_dir / name).is_file():
            continue
        if legacy:
            report.warn(
                "RESEARCH_FILE_MISSING_LEGACY",
                f"旧运行缺少必需研究文件 {name}；该运行早于研究 Schema 登记，"
                "只能按历史产物阅读，不能作为新版流程的范例",
                str(run_dir),
            )
        else:
            report.fail(
                "RESEARCH_FILE_MISSING",
                f"缺少必需研究文件 {name}；三份研究文件是完成条件，不是可选项",
                str(run_dir),
            )
            report.todos.append(f"写出 {name}（格式见 docs/implementation/schemas.md）")
    if (
        not legacy
        and not questions_path.is_file()
        and not claims_path.is_file()
        and not research_path.is_file()
    ):
        return research, claims

    def _load(name: str, path: Path) -> Any:
        try:
            return _read_json(path)
        except json.JSONDecodeError as exc:
            report.fail("RESEARCH_SCHEMA_INVALID", f"{name} 无法解析：{exc}", str(path))
            return None

    questions: list[dict[str, Any]] = []
    if questions_path.is_file():
        payload = _load("research_questions.json", questions_path)
        if payload is not None:
            if isinstance(payload, dict) and "run_id" not in payload:
                payload["run_id"] = run_id
            try:
                parsed_questions = QuestionSet.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 - pydantic text is the useful output
                report.fail(
                    "RESEARCH_SCHEMA_INVALID",
                    f"research_questions.json 不符合 Schema：{exc}",
                    str(questions_path),
                )
                parsed_questions = None
            if parsed_questions is not None:
                if parsed_questions.run_id != run_id:
                    report.fail(
                        "RESEARCH_RUN_MISMATCH",
                        f"research_questions.json 的 run_id={parsed_questions.run_id} "
                        f"与运行 {run_id} 不一致",
                        str(questions_path),
                    )
                questions = [item.model_dump() for item in parsed_questions.questions]
                seen: set[str] = set()
                for item in questions:
                    question_id = item["question_id"]
                    if question_id in seen:
                        report.fail(
                            "RESEARCH_DUPLICATE_ID",
                            f"研究问题 ID 重复：{question_id}",
                            str(questions_path),
                        )
                    seen.add(question_id)
                if len(questions) > 5:
                    report.warn(
                        "RESEARCH_TOO_MANY_QUESTIONS",
                        "关键问题超过 5 个，容易失去焦点",
                        str(questions_path),
                    )
                for item in questions:
                    if item["status"] == "unresolved" and item["priority"] == "critical":
                        report.warn(
                            "CRITICAL_QUESTION_UNRESOLVED",
                            f"关键问题仍未决：{item['question_id']}；相关结论需限定范围",
                            str(questions_path),
                        )
    elif not legacy:
        report.todos.append("写出 research_questions.json")

    claims_run_id = ""
    claims_revision_id = ""
    if claims_path.is_file():
        payload = _load("claims.json", claims_path)
        if payload is not None:
            if isinstance(payload, dict) and "run_id" not in payload:
                payload["run_id"] = run_id
            try:
                parsed_claims = ClaimSet.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                report.fail(
                    "RESEARCH_SCHEMA_INVALID",
                    f"claims.json 不符合 Schema：{exc}",
                    str(claims_path),
                )
                parsed_claims = None
            if parsed_claims is not None:
                claims_run_id = parsed_claims.run_id
                claims_revision_id = parsed_claims.revision_id
                claims = [item.model_dump() for item in parsed_claims.claims]
                if claims_run_id != run_id:
                    report.fail(
                        "RESEARCH_RUN_MISMATCH",
                        f"claims.json 的 run_id={claims_run_id} 与运行 {run_id} 不一致",
                        str(claims_path),
                    )
                seen_claims: set[str] = set()
                for claim in claims:
                    claim_id = str(claim["claim_id"])
                    if claim_id in seen_claims:
                        report.fail(
                            "RESEARCH_DUPLICATE_ID",
                            f"论点 ID 重复：{claim_id}",
                            str(claims_path),
                        )
                    seen_claims.add(claim_id)
                for claim in claims:
                    for dependency in claim.get("depends_on_claim_ids") or []:
                        if str(dependency) not in seen_claims:
                            report.fail(
                                "RESEARCH_CLAIM_UNKNOWN",
                                f"论点 {claim['claim_id']} 依赖不存在的论点 {dependency}",
                                str(claims_path),
                            )
    elif not legacy:
        report.todos.append("写出 claims.json")

    if research_path.is_file():
        payload = _load("research_decision.json", research_path)
        if payload is not None:
            if isinstance(payload, dict) and "run_id" not in payload:
                payload["run_id"] = run_id
            try:
                parsed_decision = ResearchDecision.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                report.fail(
                    "RESEARCH_DECISION_INVALID",
                    f"research_decision.json 不符合 Schema：{exc}",
                    str(research_path),
                )
                return research, claims
            research = parsed_decision.model_dump()
            if parsed_decision.run_id != run_id:
                report.fail(
                    "RESEARCH_RUN_MISMATCH",
                    f"research_decision.json 的 run_id={parsed_decision.run_id} "
                    f"与运行 {run_id} 不一致",
                    str(research_path),
                )
            if claims_revision_id and parsed_decision.revision_id != claims_revision_id:
                report.fail(
                    "RESEARCH_REVISION_MISMATCH",
                    f"研究结论基于 {parsed_decision.revision_id}，但 claims.json 是 "
                    f"{claims_revision_id}；修订后必须同步更新结论",
                    str(research_path),
                )
            known_claims = {str(claim["claim_id"]) for claim in claims}
            unknown_claims = [
                claim_id for claim_id in parsed_decision.key_claim_ids if claim_id not in known_claims
            ]
            if unknown_claims:
                report.fail(
                    "RESEARCH_CLAIM_UNKNOWN",
                    f"结论引用了不存在的论点：{', '.join(unknown_claims)}",
                    str(research_path),
                )
            known_questions = {str(item["question_id"]) for item in questions}
            unknown_questions = [
                question_id
                for question_id in parsed_decision.unresolved_question_ids
                if question_id not in known_questions
            ]
            if unknown_questions:
                report.fail(
                    "RESEARCH_QUESTION_UNKNOWN",
                    f"结论引用了不存在的研究问题：{', '.join(unknown_questions)}",
                    str(research_path),
                )
            retracted = {
                str(claim["claim_id"]) for claim in claims if claim.get("status") == "retracted"
            }
            used = set(parsed_decision.key_claim_ids) & retracted
            if used:
                report.fail(
                    "RETRACTED_CLAIM_IN_USE",
                    f"结论仍引用已撤回论点：{', '.join(sorted(used))}",
                    str(research_path),
                )
                report.todos.append(
                    "撤回论点后必须重算依赖它的估值与执行条件，再更新 research_decision"
                )
            execution = parsed_decision.execution
            if (
                execution.status == "actionable"
                and not execution.target_account_weight
                and not execution.sell_fraction_of_current
                and not execution.cash_amount
            ):
                report.fail(
                    "EXECUTION_WITHOUT_SIZE",
                    "execution_status=actionable 但没有可执行的仓位、减仓比例或金额",
                    str(research_path),
                )
            if execution.status == "actionable" and not decision.get("action"):
                report.fail("EXECUTION_WITHOUT_DECISION", "缺少 decision.json 支撑可执行状态", str(run_dir))
    elif not legacy:
        report.todos.append("写出 research_decision.json")
    _validate_role_claims(report, run_dir, claims, run)
    return research, claims


def _validate_role_claims(
    report: ValidationReport,
    run_dir: Path,
    claims: list[dict[str, Any]],
    run: dict[str, Any],
) -> None:
    """A role summary and the central claim list must not be two truths.

    The report now builds its sections from the central claims, but the role
    artifacts are still sealed and still readable, so a role that cites a claim
    id which no longer exists — or whose status changed after the role was
    written — has to be caught rather than quietly kept alongside the new text.
    """

    legacy = not run.get("analysis_schema")
    known = {str(claim.get("claim_id")) for claim in claims}
    for role_id, payload in role_payloads(run_dir).items():
        refs = role_claim_refs(payload)
        unknown = sorted(refs - known)
        if unknown:
            finding = report.warn if legacy else report.fail
            finding(
                "ROLE_CLAIM_UNKNOWN",
                f"角色 {role_id} 的摘要引用了中央论点表里不存在的 claim_id："
                f"{'、'.join(unknown)}；角色文字与论点表是两份文本，不能各说各的",
                str(run_dir),
            )
    pending = pending_role_reviews(run)
    if not pending:
        return
    for item in pending:
        finding = report.warn if legacy else report.fail
        finding(
            "ROLE_REVIEW_PENDING",
            f"角色 {item['role']} 的摘要尚未在修订 {item['revision_id']} 后重审"
            f"（涉及论点：{'、'.join(item['claim_ids']) or '未标注'}）；"
            "要么重写该角色摘要，要么用 research review --role 记录重审结论",
            str(run_dir),
        )
    if not legacy:
        report.todos.append(
            "对每个待重审角色执行 research review（--role + --revision-id），"
            "或在 agents/ 下重写该角色摘要"
        )


def _validate_gaps(report: ValidationReport, run_dir: Path, research: dict[str, Any]) -> None:
    """Gap classes must be honest and must limit the conclusions they affect."""

    path = run_dir / GAP_FILE
    if not path.is_file():
        return
    payload = _read_json(path)
    items = payload.get("gaps", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        report.fail("RESEARCH_SCHEMA_INVALID", "data_gaps.json 必须是缺口数组", str(path))
        return
    execution = research.get("execution") or {}
    for raw in items:
        if not isinstance(raw, dict):
            report.fail("RESEARCH_SCHEMA_INVALID", "缺口记录必须是对象", str(path))
            continue
        try:
            gap = GapRecord.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            report.fail("RESEARCH_SCHEMA_INVALID", f"缺口记录不合规：{exc}", str(path))
            continue
        if gap.priority != "critical":
            continue
        for dimension in gap.affects:
            if dimension in {"fundamental", "valuation", "timing"}:
                claimed = str(research.get(f"{dimension}_assessment") or "unresolved")
                if claimed != "unresolved":
                    report.fail(
                        "CRITICAL_GAP_NOT_REFLECTED",
                        f"关键缺口 {gap.gap_id}（{GAP_LABELS.get(gap.kind, gap.kind)}）仍影响"
                        f"{dimension}判断，但该维度已给出结论 {claimed}；必须改为 unresolved "
                        "或先补上证据",
                        str(path),
                    )
            if dimension == "execution" and execution.get("status") == "actionable":
                report.fail(
                    "CRITICAL_GAP_BLOCKS_EXECUTION",
                    f"关键缺口 {gap.gap_id} 影响可执行条件，但 execution 仍标为 actionable",
                    str(path),
                )


#: Which research verdict an unreviewed output would silently make stale.
OUTPUT_DIMENSIONS: dict[str, str] = {
    "valuation": "valuation",
    "valuation_reference": "valuation",
    "fair_value": "valuation",
    "target_price": "valuation",
    "fundamental": "fundamental",
    "fundamentals": "fundamental",
    "timing": "timing",
    "execution": "execution",
    "position": "execution",
}


def _validate_revisions(
    report: ValidationReport, run_dir: Path, run: dict[str, Any]
) -> list[str]:
    """An unreviewed revision invalidates the outputs it touched.

    A pending review may not be cleared in bulk: each entry names one output and
    the revision that caused it, and only a matching ``research review`` record
    closes it. While it is open, the affected verdict must be downgraded to
    ``unresolved`` instead of being published as a fresh conclusion.
    """

    records = load_revisions(run_dir)
    for record in records:
        if record.get("revision_id") == "invalid":
            report.fail(
                "RESEARCH_SCHEMA_INVALID",
                f"revisions.jsonl 存在无法解析的行：{record.get('raw', '')[:80]}",
                str(run_dir / "revisions.jsonl"),
            )
    items = pending_reviews(run)
    return [f"{item['output']}@{item['revision_id']}" for item in items]


def _check_unreviewed_outputs(
    report: ValidationReport,
    run: dict[str, Any],
    research: dict[str, Any],
    items: list[dict[str, str]],
) -> None:
    """Downgrade or fail when an unreviewed output still drives a conclusion."""

    if not items:
        return
    execution = research.get("execution") or {}
    for item in items:
        output = item["output"]
        revision_id = item["revision_id"]
        where = f"revisions.jsonl#{revision_id}"
        dimension = OUTPUT_DIMENSIONS.get(output)
        if dimension is None:
            report.fail(
                "UNREVIEWED_OUTPUT",
                f"修订 {revision_id} 影响的输出 {output} 未复核，且不在已知维度内，"
                "无法判断它是否仍被当作有效结论",
                where,
            )
            continue
        if dimension == "valuation":
            downgraded = str(research.get("valuation_assessment") or "") == "unresolved"
            still_published = research.get("valuation_reference") is not None
            if not downgraded or still_published:
                report.fail(
                    "UNREVIEWED_OUTPUT",
                    f"修订 {revision_id} 后 {output} 未复核，但估值判断仍为 "
                    f"{research.get('valuation_assessment') or '缺失'}"
                    + ("且仍保留估值参照" if still_published else "")
                    + "；未复核的估值不得作为有效结论发布",
                    where,
                )
                report.todos.append(
                    f"复核 {output}（research review）或把 valuation_assessment 改为 "
                    "unresolved 并移除 valuation_reference"
                )
            else:
                report.warn(
                    "UNREVIEWED_OUTPUT_DOWNGRADED",
                    f"{output} 因修订 {revision_id} 未复核，估值维度已降级为未定",
                    where,
                )
        elif dimension == "fundamental":
            if str(research.get("fundamental_assessment") or "") != "unresolved":
                report.fail(
                    "UNREVIEWED_OUTPUT",
                    f"修订 {revision_id} 后 {output} 未复核，但基本面判断仍为 "
                    f"{research.get('fundamental_assessment') or '缺失'}",
                    where,
                )
        elif dimension == "timing":
            if str(research.get("timing_assessment") or "") != "unresolved":
                report.fail(
                    "UNREVIEWED_OUTPUT",
                    f"修订 {revision_id} 后 {output} 未复核，但时机判断仍为 "
                    f"{research.get('timing_assessment') or '缺失'}",
                    where,
                )
        else:  # execution
            if str(execution.get("status") or "") == "actionable":
                report.fail(
                    "EXECUTION_BEFORE_REVIEW",
                    f"修订 {revision_id} 后 {output} 未复核，不能标为可执行",
                    where,
                )


FIELD_REQUIREMENTS: dict[str, set[str]] = {
    "target_account_weight": {"capital", "position_state"},
    "sell_fraction_of_current": {"position_state", "position_shares", "position_opened_date"},
    "cash_amount": {"capital"},
}
KNOWN_REQUIREMENTS = {"capital", "position_state", "position_shares", "position_opened_date"}


def _satisfied(context: TradeContext) -> dict[str, bool]:
    return {
        "capital": context.capital is not None and context.capital > 0,
        "position_state": context.position_state in {"flat", "holding"},
        "position_shares": context.position_shares is not None and context.position_shares > 0,
        "position_opened_date": bool(context.position_opened_date),
    }


def _validate_execution(
    report: ValidationReport,
    run: dict[str, Any],
    research: dict[str, Any],
    decision: dict[str, Any],
    datapack: Path,
    pending: list[str],
    risk_policy_path: Path,
) -> None:
    """Keep the account action honest: no size without inputs, no sell without stock.

    Capabilities are derived from the recorded account context, never from the
    flags the analysing agent wrote about itself.
    """

    policy = load_risk_policy(risk_policy_path)
    execution = research.get("execution") or {}
    status = str(execution.get("status") or "not_assessed")
    target = execution.get("target_account_weight")
    sell_fraction = execution.get("sell_fraction_of_current")
    cash_amount = execution.get("cash_amount")
    requires = [str(item) for item in execution.get("requires") or []]
    action = str(decision.get("action") or "")
    context = TradeContext(
        capital=(run.get("trade_context") or {}).get("capital"),
        position_state=(run.get("trade_context") or {}).get("position_state"),
        position_shares=(run.get("trade_context") or {}).get("position_shares"),
        position_opened_date=(run.get("trade_context") or {}).get("position_opened_date"),
        market=str((run.get("trade_context") or {}).get("market") or "CN"),
        board=str((run.get("trade_context") or {}).get("board") or "main"),
    )
    satisfied = _satisfied(context)
    where = str(run.get("run_dir", ""))

    for label, value in (
        ("target_account_weight", target),
        ("sell_fraction_of_current", sell_fraction),
    ):
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            report.fail("EXECUTION_SIZE_INVALID", f"{label} 不是数字：{value!r}", where)
            continue
        if value <= 0 or value > 1:
            report.fail(
                "EXECUTION_SIZE_INVALID",
                f"{label}={value} 必须落在 0~1（占总资产比例），超过 1 等于超过全部资金",
                where,
            )
    if cash_amount is not None:
        if not isinstance(cash_amount, (int, float)) or isinstance(cash_amount, bool):
            report.fail("EXECUTION_SIZE_INVALID", f"cash_amount 不是数字：{cash_amount!r}", where)
        else:
            import math

            if not math.isfinite(float(cash_amount)) or float(cash_amount) <= 0:
                report.fail(
                    "EXECUTION_SIZE_INVALID",
                    f"cash_amount={cash_amount!r} 必须是有限正数（元），不能是 0、负数或无穷",
                    where,
                )

    unknown_requires = [item for item in requires if item not in KNOWN_REQUIREMENTS]
    if unknown_requires:
        report.fail(
            "EXECUTION_REQUIREMENT_UNKNOWN",
            f"execution.requires 含无法核对的输入：{', '.join(unknown_requires)}；"
            f"可用值只有 {', '.join(sorted(KNOWN_REQUIREMENTS))}",
            where,
        )

    used_fields = [
        name
        for name, value in (
            ("target_account_weight", target),
            ("sell_fraction_of_current", sell_fraction),
            ("cash_amount", cash_amount),
        )
        if value is not None
    ]
    needed: set[str] = {item for item in requires if item in KNOWN_REQUIREMENTS}
    for field_name in used_fields:
        needed |= FIELD_REQUIREMENTS[field_name]
    missing_inputs = sorted(name for name in needed if not satisfied.get(name, False))
    # Declaring ``requires`` on a blocked plan is the honest move, so it must not
    # be what fails the run; what fails is claiming a size (or actionable) while
    # the inputs behind that size are absent.
    if missing_inputs and (status == "actionable" or used_fields):
        report.fail(
            "EXECUTION_CONTEXT_MISSING",
            f"执行方案需要 {', '.join(missing_inputs)}，但账户上下文未提供或不可用；"
            "公司研究可以继续，仓位与金额不得写成可执行",
            where,
        )
        report.todos.append(
            "补齐账户资金/持仓状态/可卖股数与开仓日期后重新计算仓位，"
            "或把 execution.status 改为 blocked 并写明 block_reasons"
        )
    elif missing_inputs:
        report.warn(
            "EXECUTION_CONTEXT_MISSING",
            f"执行字段依赖 {', '.join(missing_inputs)}，当前上下文缺失；执行状态不是 actionable，"
            "这些数字只能当作情景，不能执行",
            where,
        )

    if status != "actionable":
        _compare_position_sources(report, decision, target, cash_amount, context, policy, where)
        return
    if action not in {"BUY", "SELL"}:
        report.fail(
            "EXECUTION_WITHOUT_ACTION",
            f"execution_status=actionable 但 decision.action={action or '缺失'}；"
            "没有买卖动作就没有可执行仓位",
            where,
        )
    if target is None and sell_fraction is None and cash_amount is None:
        report.fail(
            "EXECUTION_WITHOUT_SIZE",
            "execution_status=actionable 但没有 target_account_weight / "
            "sell_fraction_of_current / cash_amount",
            where,
        )
    if action == "BUY" and sell_fraction is not None:
        report.fail(
            "EXECUTION_DIRECTION_MISMATCH",
            "买入动作不得使用 sell_fraction_of_current（占现持仓比例）",
            where,
        )
    if action == "SELL" and (target is not None or cash_amount is not None):
        report.fail(
            "EXECUTION_DIRECTION_MISMATCH",
            "卖出动作只能使用 sell_fraction_of_current；target_account_weight / cash_amount "
            "是买入口径，混用会把减仓写成加仓",
            where,
        )
    if action == "SELL" and str(context.position_state or "") != "holding":
        report.fail(
            "SELL_WITHOUT_POSITION",
            "公司研究给出卖出动作，但账户没有持仓可卖；这只能是“不建议买入/继续观察”，"
            "不能写成卖出指令",
            where,
        )
    if pending:
        report.fail(
            "EXECUTION_BEFORE_REVIEW",
            f"修订后的输出尚未复核（{', '.join(pending)}），不能标为可执行",
            where,
        )
    if datapack.is_dir() and tradability_status(datapack) == "unknown":
        report.fail(
            "TRADE_STATE_UNKNOWN",
            "无法确认该标的当日可交易状态（停牌/涨跌停/无行情），不能标为可执行",
            str(datapack),
        )
    _compare_position_sources(report, decision, target, cash_amount, context, policy, where)


def _compare_position_sources(
    report: ValidationReport,
    decision: dict[str, Any],
    target: float | None,
    cash_amount: float | None,
    context: TradeContext,
    policy: Any,
    where: str,
) -> None:
    """The new execution fields and the legacy sizing must describe one position."""

    capital = context.capital if context.capital and context.capital > 0 else None
    weight = None
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        weight = float(target)
    elif isinstance(cash_amount, (int, float)) and capital:
        weight = float(cash_amount) / capital
    if weight is None:
        return
    if weight > policy.max_position_pct / 100 + 1e-9:
        report.fail(
            "EXECUTION_SIZE_EXCEEDS_POLICY",
            f"目标账户权重 {weight:.2%} 超过风控上限 {policy.max_position_pct:.0f}%",
            where,
        )
    legacy_pct = decision.get("position_pct")
    if (
        isinstance(legacy_pct, (int, float))
        and not isinstance(legacy_pct, bool)
        and abs(float(legacy_pct) / 100 - weight) > 0.005
    ):
        report.fail(
            "POSITION_SOURCE_CONFLICT",
            f"decision.position_pct={legacy_pct}% 与执行方案目标权重 {weight:.2%} 不一致；"
            "同一仓位不能有两个数字",
            where,
        )
    amount = decision.get("position_amount")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        if capital and isinstance(cash_amount, (int, float)):
            if abs(float(cash_amount) - float(amount)) > max(1.0, 0.02 * float(cash_amount)):
                report.fail(
                    "POSITION_SOURCE_CONFLICT",
                    f"decision.position_amount={amount} 与 cash_amount={cash_amount} 不一致",
                    where,
                )
        elif capital:
            expected = capital * weight
            if abs(float(amount) - expected) > max(1.0, 0.02 * expected):
                report.fail(
                    "POSITION_SOURCE_CONFLICT",
                    f"decision.position_amount={amount} 与 capital×目标权重={expected:.2f} 不一致",
                    where,
                )


def _validate_valuation(report: ValidationReport, research: dict[str, Any]) -> None:
    reference = research.get("valuation_reference")
    if not isinstance(reference, dict):
        return
    method = str(reference.get("method") or "none")
    if method == "historical_median_multiple":
        required = ("window_start", "window_end", "sample_size", "percentile_definition")
        missing = [name for name in required if reference.get(name) in (None, "")]
        if missing:
            report.fail(
                "VALUATION_PERCENTILE_UNDEFINED",
                "历史分位估值缺少窗口起止/样本数/分位定义：" + "、".join(missing),
                str(report.run_id),
            )


def _validate_rule_tree(
    report: ValidationReport,
    run_dir: Path,
    research: dict[str, Any],
) -> None:
    """A rule tree may only drive an action when it is internally consistent."""

    path = run_dir / RULE_FILE
    try:
        tree = load_rule_tree(path)
    except Exception as exc:  # noqa: BLE001
        report.fail("RULE_SCHEMA_INVALID", f"trading_rules.json 不符合 Schema：{exc}", str(path))
        return
    if tree is None:
        return
    errors, notes = validate_rule_tree(tree, available_data="daily")
    for message in errors:
        report.fail(message.split(":", 1)[0], message, str(path))
    for note in notes:
        report.note(note.split(":", 1)[0], note, str(path))
    if errors and (research.get("execution") or {}).get("status") == "actionable":
        report.fail(
            "RULE_TREE_BLOCKS_ACTION",
            "交易规则树存在冲突或未验证触发，不能标为可执行",
            str(path),
        )


def _validate_dividend_treatment(report: ValidationReport, research: dict[str, Any]) -> None:
    if str(research.get("dividend_treatment") or "none") == "assumption":
        report.warn(
            "DIVIDEND_ASSUMPTION",
            "股息按假设处理，不得把滚动股息率乘持有月数当作确定现金流，也不得进入主收益表",
            str(report.run_id),
        )


def submit_analysis(config: ProjectConfig, run_id: str) -> dict[str, Any]:
    """Move a prepared run to ``DECIDED`` once its products exist and parse.

    This exists so nobody has to hand-edit ``run.json``: the agent saves the
    role outputs and research files, then runs this command, which checks them
    and records the transition. ``validate`` still decides whether the run is
    complete.
    """

    run_dir = config.root / config.app.run_dir / run_id
    state_path = run_dir / "run.json"
    if not state_path.is_file():
        return {"ok": False, "reason": f"运行不存在：{run_dir}"}
    run = _read_json(state_path)
    status = str(run.get("status") or "")
    if status in {"FINALIZED", "AUDITED"}:
        return {"ok": True, "status": status, "reason": "已完成封存，无需再次提交"}
    if str(run.get("mode") or "") == "data":
        return {"ok": False, "status": status, "reason": "mode=data 的运行不产生研究结论"}

    report = ValidationReport(run_id=run_id)
    request_path = run_dir / "current_agent_request.json"
    if not request_path.is_file():
        report.fail("REQUEST_MISSING", "缺少 current_agent_request.json", str(run_dir))
    else:
        _check_role_outputs(report, _read_json(request_path), run_dir)

    decision: dict[str, Any] = {}
    decision_path = run_dir / "decision.json"
    if not decision_path.is_file():
        report.fail("DECISION_MISSING", "缺少 decision.json", str(run_dir))
    else:
        decision = _read_json(decision_path)
        try:
            FinalDecision.model_validate(decision)
        except Exception as exc:  # noqa: BLE001
            report.fail(
                "DECISION_SCHEMA_INVALID", f"decision.json 不符合 Schema：{exc}", str(decision_path)
            )
    _validate_research(report, run_dir, decision, run)

    record = {
        "schema_version": "1.0",
        "run_id": run_id,
        "ok": report.ok,
        "submitted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "failures": [finding.to_dict() for finding in report.findings if finding.level == "FAIL"],
        "todos": report.todos,
    }
    (run_dir / "submit.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if report.ok:
        run["status"] = "DECIDED"
        run["updated_at"] = record["submitted_at"]
        state_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        record["status"] = "DECIDED"
    else:
        record["status"] = status
    return record


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
    if status in PREPARED_STATUSES:
        # A prepared run is not automatically "not ready": judge by the
        # artifacts, not by the flag the preparation step wrote. A data-only
        # run has no analysis products by definition and still fails.
        if str(run.get("mode") or "") == "data":
            report.fail(
                "ANALYSIS_NOT_READY",
                "这是只取数据的运行（mode=data），不产生研究结论，不能校验为完成",
                str(state_path),
            )
            report.todos.append("用 mode=full 重新 prepare/analyze 后再分析并 validate")
            return report
        request_path = run_dir / "current_agent_request.json"
        if not request_path.is_file():
            report.fail(
                "REQUEST_MISSING",
                "运行处于准备状态且缺少 current_agent_request.json，无法判断应有产物",
                str(run_dir),
            )
            return report
        missing = _analysis_artifacts(run_dir, _read_json(request_path))
        if missing:
            # Keep going: the per-artifact checks below name each missing piece,
            # so the operator sees what to write instead of a bare status code.
            report.fail(
                "ANALYSIS_NOT_READY",
                "运行仍处于准备状态，缺少分析产物：" + "、".join(missing),
                str(state_path),
            )
            report.todos.append("补齐上述产物；可用 submit 推进状态，再 validate")
        else:
            report.note(
                "ANALYSIS_PRODUCTS_PRESENT",
                "运行状态仍是准备态，但分析产物齐全；本次按产物而非状态判定，"
                "通过后会写入 VALIDATED",
                str(state_path),
            )

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

    research, claims = _validate_research(report, run_dir, decision, run)
    _validate_gaps(report, run_dir, research)
    _validate_valuation(report, research)
    pending_entries = pending_reviews(run)
    pending_labels = _validate_revisions(report, run_dir, run)
    _check_unreviewed_outputs(report, run, research, pending_entries)
    _validate_execution(
        report,
        run,
        research,
        decision,
        datapack,
        pending_labels,
        config.root / "configs" / "risk.yaml",
    )
    _validate_rule_tree(report, run_dir, research)
    _validate_dividend_treatment(report, research)

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
        if is_after(item.get("available_at"), asof):
            report.fail(
                "EVIDENCE_FUTURE",
                f"证据 {evidence_id} 在 {to_iso_date(item.get('available_at'))} 才可得，"
                f"晚于基准日 {to_iso_date(asof)}",
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
        _verify_financials(report, datapack, run)
    else:
        report.fail("DATAPACK_MISSING", f"数据包目录不存在：{datapack}", str(run_dir))

    if not report.ok and not report.todos:
        report.todos.append("按失败项逐条修复后重新运行 validate")
    context = TradeContext(
        capital=(run.get("trade_context") or {}).get("capital"),
        position_state=(run.get("trade_context") or {}).get("position_state"),
        position_shares=(run.get("trade_context") or {}).get("position_shares"),
        position_opened_date=(run.get("trade_context") or {}).get("position_opened_date"),
        market=str((run.get("trade_context") or {}).get("market") or "CN"),
        board=str((run.get("trade_context") or {}).get("board") or "main"),
    )
    report.capabilities = _satisfied(context)
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
    # The revision log is printed in the report, so its "reviewed" flags are
    # derived from the pending queues right before the report is written.
    reconcile_revisions(run_dir)
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
    signed_run = _read_json(run_dir / "run.json")
    seal = build_seal(run_dir, signed_run)
    record.update(
        {
            "ok": True,
            "stage": "finalized",
            "report_html_sha256": sha256_file(report_html),
            "report_md_sha256": sha256_file(report_md),
            "finalized_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "seal": seal,
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
