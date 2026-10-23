"""Explicit asyncio workflow for the Trade-Council expert team."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import ProjectConfig
from ..data.supplement import DataNeed, WebSearchProvider, detect_data_needs, supplement_data_needs
from ..decision.risk_rules import (
    TradeContext,
    enrich_decision_with_trade_context,
    load_risk_policy,
    validate_decision,
)
from ..engineering.evidence import EvidenceIndex, EvidenceItem
from ..errors import InvalidResponseError
from ..extensions.loader import load_resolved_methods
from ..extensions.runtime import build_role_method_context, execute_method
from ..llm.base import Message
from ..llm.client import LLMClient
from ..llm.prompts import PromptRegistry
from ..operations.runs import summarize_calls
from ..schemas import (
    DecisionPlan,
    FinalDecision,
    RoleOutput,
    TradeProposal,
)
from .run_store import RunStore
from .screening import make_screening_result

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)
_EVIDENCE_CLAIM_RULES = (
    (("龙虎榜",), "sentiment.top_list", {"sentiment.top_list"}),
    (("两融", "融资融券"), "margin", {"sentiment.margin_detail"}),
    (("主力", "超大单", "大单", "资金流"), "moneyflow", {"market.moneyflow"}),
    (("PE", "PB", "PS", "估值"), "valuation", {"market.daily_basic"}),
    (
        ("均线", "MACD", "RSI", "BOLL", "ATR", "支撑位", "压力位", "收盘", "涨停"),
        "price",
        {"market.daily"},
    ),
    (
        ("营收", "净利", "现金流", "资产负债率", "毛利", "应收", "存货"),
        "financial",
        {"financial.statements"},
    ),
    (("大宗交易",), "block", {"sentiment.block_trade"}),
    (("北向",), "northbound", {"sentiment.hsgt_top10"}),
)


class WorkflowEngine:
    def __init__(
        self,
        config: ProjectConfig,
        *,
        data_pack_dir: Path,
        run_dir: Path,
        profile: str,
        mode: str,
        deep_tier: str | None,
        include_news: bool,
        trade_context: TradeContext | None = None,
        run_kind: str = "normal",
        web_search: bool = False,
        web_search_provider: WebSearchProvider | None = None,
        resume: bool = False,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.data_pack_dir = data_pack_dir.expanduser().resolve()
        self.store = RunStore(run_dir)
        self.profile = profile
        self.mode = mode
        self.deep_tier = deep_tier
        self.include_news = include_news
        self.trade_context = trade_context or TradeContext()
        self.run_kind = run_kind
        self.web_search = web_search
        self.web_search_provider = web_search_provider
        self.resume = resume
        self.prompts = PromptRegistry(config.root / "prompts")
        self.client = LLMClient(
            config.models,
            providers=providers,
            trace_sink=self.store.append_trace_call,
        )
        self.methods = load_resolved_methods(config)
        self.evidence = EvidenceIndex.load(self.data_pack_dir / "evidence.json")
        supplemental_evidence_path = self.store.run_dir / "supplemental_evidence.json"
        if supplemental_evidence_path.is_file():
            supplemental = json.loads(
                supplemental_evidence_path.read_text(encoding="utf-8")
            )
            for item in supplemental.get("items", []):
                self.evidence.add_item(EvidenceItem.model_validate(item))
        self.digest = (self.data_pack_dir / "DATA_DIGEST.md").read_text(encoding="utf-8")
        self.manifest = json.loads(
            (self.data_pack_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.roles = {role.id: role for role in config.team.roles}
        self.data_needs: list[DataNeed] = []
        self.web_supplement_status = "not_run"
        self.prompt_hashes = {
            role.id: self.prompts.load(role.id).sha256
            for role in config.team.roles
        }
        referenced_methods = {
            method_id
            for role in config.team.roles
            for method_id in role.methods
        }
        self.method_executions = self._resolve_method_executions(
            mode,
            referenced_methods,
        )
        self.capability_flags: dict[str, bool] = {
            "news": include_news,
            "position_size": self.trade_context.position_size_allowed,
            "trade_instructions": False,
        }
        profile_config = self.config.models.profiles[self.profile]
        self.allow_trade_instructions = profile_config.allow_trade_instructions

    def _resolve_method_executions(
        self,
        mode: str,
        referenced_methods: set[str],
    ) -> dict[str, dict[str, Any]]:
        if mode != "full":
            return {}
        methods = {
            method.manifest.id: method
            for method in self.methods
            if method.manifest.id in referenced_methods
        }
        executions: dict[str, dict[str, Any]] = {}
        cached_path = self.store.run_dir / "method_results.json"
        if self.resume and cached_path.is_file():
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
            for item in cached:
                method = methods.get(str(item.get("id")))
                if method is None:
                    continue
                manifest_sha = hashlib.sha256(method.path.read_bytes()).hexdigest()
                method_md = method.path.parent / "method.md"
                method_md_sha = (
                    hashlib.sha256(method_md.read_bytes()).hexdigest()
                    if method_md.is_file()
                    else None
                )
                if (
                    item.get("manifest_sha256") == manifest_sha
                    and item.get("method_md_sha256") == method_md_sha
                    and item.get("snapshot_id") == self.manifest.get("snapshot_id")
                ):
                    executions[method.manifest.id] = item
        for method_id, method in methods.items():
            if method_id in executions:
                continue
            executions[method_id] = execute_method(
                method,
                data_pack_dir=self.data_pack_dir,
                evidence=self.evidence,
                manifest=self.manifest,
            )
        return executions

    async def run(self) -> dict[str, Any]:
        state = self.store.load_state() if self.resume else {}
        if state.get("failed_phase"):
            state.setdefault("failure_history", []).append(
                {
                    "phase": state.get("failed_phase"),
                    "error": state.get("error"),
                    "recorded_at": state.get("updated_at"),
                }
            )
            state.pop("failed_phase", None)
            state.pop("error", None)
        now = datetime.now().isoformat(timespec="seconds")
        state.setdefault("started_at", now)
        state.update(
            {
                "run_id": self.store.run_dir.name,
                "mode": self.mode,
                "profile": self.profile,
                "deep_tier": self.deep_tier,
                "symbol": self.manifest["ts_code"],
                "stock_name": self.manifest.get("stock_name", self.manifest["ts_code"]),
                "asof": self.manifest["asof"],
                "snapshot_id": self.manifest.get("snapshot_id"),
                "data_pack_dir": str(self.data_pack_dir),
                "run_kind": state.get("run_kind", self.run_kind) if self.resume else self.run_kind,
                "status": "ANALYZING",
                "updated_at": now,
                "completed_phases": state.get("completed_phases", []),
                "capability_flags": self.capability_flags,
                "trade_context": asdict(self.trade_context),
                "prompt_hashes": self.prompt_hashes,
                "degraded": state.get("degraded", False),
            }
        )
        state["completed_phases"] = list(dict.fromkeys(state.get("completed_phases", [])))
        self.store.save_state(state)
        logger.info(
            "Workflow started run_id=%s mode=%s profile=%s symbol=%s asof=%s",
            self.store.run_dir.name,
            self.mode,
            self.profile,
            self.manifest.get("ts_code"),
            self.manifest.get("asof"),
        )
        self.store.write_json(
            "resolved_methods.json",
            [
                {
                    "id": method.manifest.id,
                    "version": method.manifest.version,
                    "domain": method.manifest.domain,
                    "enabled": method.enabled,
                    "path": str(method.path),
                    "manifest_sha256": hashlib.sha256(
                        method.path.read_bytes()
                    ).hexdigest(),
                    "method_md_sha256": (
                        hashlib.sha256(
                            (method.path.parent / "method.md").read_bytes()
                        ).hexdigest()
                        if (method.path.parent / "method.md").is_file()
                        else None
                    ),
                }
                for method in self.methods
            ],
        )
        self.store.write_json(
            "method_results.json",
            list(self.method_executions.values()),
        )
        state["method_results"] = [
            {
                "id": item["id"],
                "version": item["version"],
                "status": item["status"],
                "data_gaps": item["data_gaps"],
                "errors": item["errors"],
            }
            for item in self.method_executions.values()
        ]
        state["degraded"] = state["degraded"] or self._has_method_gaps()

        if self.mode == "full":
            await self._supplement_missing_data(state)

        if self.mode == "quick":
            screening = make_screening_result(self.evidence)
            self.store.write_json("screening_result.json", screening.model_dump())
            state.update(
                {
                    "status": "COMPLETED_NO_DECISION",
                    "screening_result": screening.model_dump(),
                }
            )
            self._update_telemetry(state)
            self.store.save_state(state)
            logger.info("Workflow completed run_id=%s status=%s", self.store.run_dir.name, state["status"])
            return state

        if self.mode == "data":
            state["status"] = "DATA_READY"
            self._update_telemetry(state)
            self.store.save_state(state)
            logger.info("Workflow completed run_id=%s status=%s", self.store.run_dir.name, state["status"])
            return state

        try:
            phase1 = await self._phase1()
        except Exception as exc:
            self._record_failure(state, "phase1", exc)
            raise
        logger.info("Phase 1 complete")
        self._mark_phase(state, "phase1")
        state["degraded"] = self._has_method_gaps() or self._has_gaps(phase1.values())
        self.store.save_state(state)

        try:
            phase2 = await self._phase2(phase1)
        except Exception as exc:
            self._record_failure(state, "phase2", exc)
            raise
        logger.info("Phase 2 complete")
        self._mark_phase(state, "phase2")
        state["degraded"] = (
            state["degraded"]
            or self._has_method_gaps()
            or self._has_gaps(phase2.values())
        )
        self.store.save_state(state)

        try:
            phase3 = await self._phase3(phase1, phase2)
        except Exception as exc:
            self._record_failure(state, "phase3", exc)
            raise
        logger.info("Phase 3 complete")
        self._mark_phase(state, "phase3")
        state["degraded"] = (
            state["degraded"]
            or self._has_method_gaps()
            or self._has_gaps(phase3.values())
        )
        self.store.save_state(state)

        try:
            phase4, final_decision = await self._phase4(phase1, phase2, phase3)
        except Exception as exc:
            self._record_failure(state, "phase4", exc)
            raise
        final_decision = self._apply_profile_action_policy(final_decision)
        logger.info("Phase 4 complete")
        self._mark_phase(state, "phase4")
        state["degraded"] = (
            state["degraded"]
            or self._has_method_gaps()
            or self._has_gaps(phase4.values())
        )
        decision_check = validate_decision(
            final_decision.model_dump(),
            self.data_pack_dir,
            load_risk_policy(self.config.root / "configs" / "risk.yaml"),
            allow_trade_instructions=self.allow_trade_instructions,
            trade_context=self.trade_context,
            market_rules_path=self.config.root / "configs" / "market-rules.yaml",
            asof=str(self.manifest.get("asof", "")),
        )
        state["decision_check"] = {
            "ok": decision_check.ok,
            "errors": decision_check.errors,
            "warnings": decision_check.warnings,
            "disclosures": decision_check.disclosures,
            "rule_checks": decision_check.rule_checks,
        }
        state["capability_flags"]["trade_instructions"] = (
            self.allow_trade_instructions
            and final_decision.action in {"BUY", "SELL"}
            and decision_check.ok
        )
        state["capability_flags"]["position_size"] = (
            self.trade_context.position_size_allowed
            and final_decision.action in {"BUY", "SELL"}
            and decision_check.ok
        )
        decision_payload = final_decision.model_dump()
        decision_payload["rule_checks"] = decision_check.rule_checks
        decision_payload["capability_flags"] = dict(self.capability_flags)
        state["final_decision"] = decision_payload
        state["status"] = "DECIDED"
        self._update_telemetry(state)
        self.store.write_json("decision.json", decision_payload)
        self.store.save_state(state)
        logger.info(
            "Workflow completed run_id=%s status=%s action=%s",
            self.store.run_dir.name,
            state["status"],
            final_decision.action,
        )
        return state

    def _update_telemetry(self, state: dict[str, Any]) -> None:
        summary = summarize_calls(self.store.run_dir)
        call_path = self.store.run_dir / "model_calls.jsonl"
        calls = (
            [
                json.loads(line)
                for line in call_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if call_path.is_file()
            else []
        )
        state["cost"] = {
            "prompt_tokens": summary["prompt_tokens"],
            "completion_tokens": summary["completion_tokens"],
            "total_tokens": summary["total_tokens"],
            "estimated_cost": summary["estimated_cost"],
            "unknown_pricing_calls": summary["unknown_priced_calls"],
            "calls": summary["calls"],
        }
        state["model_digests"] = sorted(
            {
                str(call["model_digest"])
                for call in calls
                if call.get("model_digest")
            }
        )
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.store.write_json("costs.json", summary)

    def _record_failure(
        self,
        state: dict[str, Any],
        phase: str,
        exc: Exception,
    ) -> None:
        logger.exception("Workflow failed in %s", phase)
        self._update_telemetry(state)
        state.update(
            {
                "status": "FAILED",
                "failed_phase": phase,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        )
        self.store.save_state(state)

    async def _phase1(self) -> dict[str, dict[str, Any]]:
        role_ids = [
            role.id
            for role in self.config.team.roles
            if role.stage == "phase1"
            and (role.enabled or (role.id == "news-analyst" and self.include_news))
        ]
        if not self.include_news:
            role_ids = [role_id for role_id in role_ids if role_id != "news-analyst"]
            self.capability_flags["news"] = False
        tasks = [
            self._run_role(
                phase="phase1",
                role_id=role_id,
                schema=RoleOutput,
                prior={},
            )
            for role_id in role_ids
        ]
        results = await asyncio.gather(*tasks)
        return {role_id: result for role_id, result in zip(role_ids, results, strict=True)}

    async def _phase2(
        self, phase1: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        bull = await self._run_role(
            phase="phase2",
            role_id="bull-researcher",
            schema=RoleOutput,
            prior={"phase1": phase1},
        )
        bear = await self._run_role(
            phase="phase2",
            role_id="bear-researcher",
            schema=RoleOutput,
            prior={"phase1": phase1, "bull": bull},
        )
        manager = await self._run_role(
            phase="phase2",
            role_id="research-manager",
            schema=DecisionPlan,
            prior={"phase1": phase1, "bull": bull, "bear": bear},
        )
        return {"bull-researcher": bull, "bear-researcher": bear, "research-manager": manager}

    async def _phase3(
        self,
        phase1: dict[str, dict[str, Any]],
        phase2: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        trader = await self._run_role(
            phase="phase3",
            role_id="trader",
            schema=TradeProposal,
            prior={"phase1": phase1, "phase2": phase2},
        )
        return {"trader": trader}

    async def _phase4(
        self,
        phase1: dict[str, dict[str, Any]],
        phase2: dict[str, dict[str, Any]],
        phase3: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], FinalDecision]:
        risk_roles = [
            "aggressive-risk-analyst",
            "conservative-risk-analyst",
            "neutral-risk-analyst",
        ]
        tasks = [
            self._run_role(
                phase="phase4",
                role_id=role_id,
                schema=RoleOutput,
                prior={"phase1": phase1, "phase2": phase2, "phase3": phase3},
            )
            for role_id in risk_roles
        ]
        results = await asyncio.gather(*tasks)
        risk_outputs = {
            role_id: result for role_id, result in zip(risk_roles, results, strict=True)
        }
        final_raw = await self._run_role(
            phase="phase4",
            role_id="risk-manager",
            schema=FinalDecision,
            prior={
                "phase1": phase1,
                "phase2": phase2,
                "phase3": phase3,
                "risk_analysts": risk_outputs,
            },
        )
        final = FinalDecision.model_validate(final_raw)
        return risk_outputs, final

    async def _run_role(
        self,
        *,
        phase: str,
        role_id: str,
        schema: type[T],
        prior: dict[str, Any],
    ) -> dict[str, Any]:
        if self.resume:
            cached = self.store.load_role(phase, role_id)
            if cached is not None:
                cached_value = schema.model_validate(cached)
                return self._normalize_decision(schema, cached_value).model_dump()

        role = self.roles[role_id]
        logger.info("Running role %s in %s", role_id, phase)
        method_context = build_role_method_context(
            self.method_executions,
            role.methods,
        )
        prompt = self.prompts.load(role_id).text
        system = (
            prompt
            + "\n\n只输出一个 JSON 对象，不要 Markdown 代码围栏。"
            + "\n必须返回完整对象，不能只返回 role_id。"
            + "\n关键论点最多 6 条，避免重复和长篇复述。"
            + f"\n字段示例：{self._schema_hint(schema)}"
            + f"\n输出必须符合以下 JSON Schema:\n{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        )
        if role_id in {"bull-researcher", "bear-researcher"}:
            system += "\n多空辩论最多 4 个论点，每个论点不超过 160 字。"
        if not self.allow_trade_instructions:
            system += (
                "\n当前 profile 不允许可执行交易指令，最终 action 不得为 BUY 或 SELL；"
                "只能使用 HOLD 或 INSUFFICIENT_DATA。"
            )
        user_payload = {
            "role_id": role_id,
            "symbol": self.manifest.get("ts_code"),
            "asof": self.manifest.get("asof"),
            "trade_context": asdict(self.trade_context),
            "position_size_allowed": self.trade_context.position_size_allowed,
            "trade_instructions_allowed": self.allow_trade_instructions,
            "data_needs": [item.model_dump() for item in self.data_needs],
            "web_supplement_status": self.web_supplement_status,
            "data_digest": self.digest,
            "evidence": [item.model_dump() for item in self.evidence.all()],
            "method_context": method_context,
            "prior": self._compact_prior(prior),
        }
        messages = [
            Message(role="system", content=system),
            Message(role="user", content=json.dumps(user_payload, ensure_ascii=False)),
        ]
        validated: T | None = None
        last_error: InvalidResponseError | None = None
        for attempt in range(2):
            try:
                parsed, result, _snapshot = await self.client.complete_json(
                    role=role_id,
                    profile=self.profile,
                    phase=phase,
                    deep_tier=self.deep_tier,
                    messages=messages,
                    max_tokens=self.config.models.profiles[self.profile].max_tokens,
                    timeout_seconds=self.config.models.profiles[
                        self.profile
                    ].request_timeout_seconds,
                    reasoning_effort=self.config.models.profiles[
                        self.profile
                    ].reasoning_effort,
                )
            except InvalidResponseError as exc:
                last_error = exc
                if attempt == 0:
                    logger.warning(
                        "Retrying role %s after unparseable model response: %s",
                        role_id,
                        exc,
                    )
                    messages = [
                        *messages,
                        Message(
                            role="user",
                            content=(
                                "上一轮响应不是可解析的完整 JSON。"
                                f"错误：{exc}\n"
                                "请只输出一个完整 JSON 对象，不要 Markdown、不要解释、不要截断。"
                            ),
                        ),
                    ]
                    continue
                raise
            if "role_id" in schema.model_fields:
                parsed["role_id"] = role_id
            try:
                candidate = schema.model_validate(parsed)
                candidate = self._normalize_decision(schema, candidate)
                self._validate_evidence(candidate)
                validated = candidate
                break
            except ValidationError as exc:
                last_error = InvalidResponseError(
                    f"Invalid {schema.__name__} from {role_id}: {exc}; "
                    f"raw={result.content[:800]}"
                )
            except InvalidResponseError as exc:
                last_error = exc
            if attempt == 0:
                logger.warning(
                    "Retrying role %s after structured validation failure: %s",
                    role_id,
                    last_error,
                )
                messages = [
                    *messages,
                    Message(role="assistant", content=result.content),
                    Message(
                        role="user",
                        content=(
                            "上一轮输出未通过结构化或证据门禁："
                            f"{last_error}\n"
                            "请重新输出完整 JSON；修正 Schema，并确保每条 key_finding "
                            "引用与论点语义对应的真实 evidence_id。"
                        ),
                    ),
                ]
        if validated is None:
            raise last_error or InvalidResponseError(
                f"Invalid {schema.__name__} from {role_id}"
            )
        markdown = self._markdown_for(role_id, validated)
        self.store.write_role(
            phase=phase,
            role_id=role_id,
            structured=validated.model_dump(),
            markdown=markdown,
        )
        _ = result
        return validated.model_dump()

    async def _supplement_missing_data(self, state: dict[str, Any]) -> None:
        needs = detect_data_needs(self.manifest, include_news=self.include_news)
        self.data_needs = needs
        provider = self.web_search_provider if self.web_search else None
        payload = await supplement_data_needs(
            needs=needs,
            provider=provider,
            data_config=self.config.data,
            evidence=self.evidence,
            asof=str(self.manifest.get("asof", "")),
            symbol=str(self.manifest.get("ts_code", "")),
            stock_name=str(self.manifest.get("stock_name") or self.manifest.get("ts_code", "")),
            run_dir=self.store.run_dir,
            allow_web=self.web_search,
        )
        self.web_supplement_status = str(payload.get("status", "unknown"))
        state["data_needs"] = [item.model_dump() for item in needs]
        state["web_supplement"] = payload
        self.capability_flags["web_supplement"] = self.web_supplement_status in {
            "not_needed",
            "completed",
        }
        if needs and not self.capability_flags["web_supplement"]:
            state["degraded"] = True
        self.store.save_state(state)

    def _normalize_decision(self, schema: type[T], value: T) -> T:
        if not isinstance(value, (TradeProposal, FinalDecision)):
            return value
        normalized = enrich_decision_with_trade_context(
            value.model_dump(),
            self.trade_context,
            self.config.root / "configs" / "market-rules.yaml",
        )
        return schema.model_validate(normalized)

    def _apply_profile_action_policy(self, decision: FinalDecision) -> FinalDecision:
        data = decision.model_dump()
        data["degraded_from_action"] = None
        trade_fields = (
            "entry_zone",
            "target_price",
            "stop_loss",
            "position_pct",
            "position_shares",
            "position_amount",
            "position_opened_date",
        )
        if decision.action not in {"BUY", "SELL"}:
            for field_name in trade_fields:
                data[field_name] = None
            return FinalDecision.model_validate(data)
        if self.allow_trade_instructions:
            return FinalDecision.model_validate(data)
        original_action = decision.action
        for field_name in trade_fields:
            data[field_name] = None
        data["action"] = "INSUFFICIENT_DATA"
        data["confidence"] = "low"
        data["degraded_from_action"] = original_action
        data["rationale"] = (
            f"模型原动作 {original_action} 被当前 profile 禁止，已降级为不可执行决策。\n"
            + decision.rationale
        )
        data["data_gaps"] = list(
            dict.fromkeys(
                [
                    *decision.data_gaps,
                    f"profile rejected executable action: {original_action}",
                ]
            )
        )
        return FinalDecision.model_validate(data)

    @staticmethod
    def _mark_phase(state: dict[str, Any], phase: str) -> None:
        if phase not in state["completed_phases"]:
            state["completed_phases"].append(phase)

    @classmethod
    def _compact_prior(cls, value: Any) -> Any:
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"scores", "risks", "data_gaps", "summary"}:
                    compact[key] = item
                elif key == "key_findings" and isinstance(item, list):
                    compact[key] = [
                        {
                            k: finding.get(k)
                            for k in ("claim_id", "claim", "evidence_ids", "kind", "status")
                        }
                        for finding in item
                    ]
                elif key in {
                    "action",
                    "confidence",
                    "rationale",
                    "stance",
                    "role_id",
                    "entry_zone",
                    "target_price",
                    "stop_loss",
                    "position_pct",
                    "position_shares",
                    "position_amount",
                    "position_opened_date",
                    "position_size_blocked_reason",
                    "capability_flags",
                    "evidence_ids",
                    "key_points",
                }:
                    compact[key] = item
                else:
                    compact[key] = cls._compact_prior(item)
            return compact
        if isinstance(value, list):
            return [cls._compact_prior(item) for item in value]
        return value

    @staticmethod
    def _schema_hint(schema: type[BaseModel]) -> str:
        if schema is RoleOutput:
            return (
                '{"role_id":"role","stance":"neutral","summary":"...",'
                '"key_findings":[],"risks":[],"data_gaps":[],"scores":{}}'
            )
        if schema is DecisionPlan:
            return (
                '{"action":"HOLD","confidence":"medium","rationale":"...",'
                '"key_points":[],"evidence_ids":[],"data_gaps":[]}'
            )
        if schema is TradeProposal:
            return (
                '{"action":"HOLD","confidence":"medium","entry_zone":null,'
                '"target_price":null,"stop_loss":null,"position_pct":null,'
                '"position_shares":null,"position_amount":null,'
                '"position_opened_date":null,'
                '"position_size_blocked_reason":"...","rationale":"...",'
                '"key_points":[],"evidence_ids":[],"data_gaps":[]}'
            )
        return (
            '{"action":"HOLD","confidence":"medium","risk_level":"medium",'
            '"rationale":"...","entry_zone":null,"target_price":null,'
            '"stop_loss":null,"position_pct":null,"position_shares":null,'
            '"position_amount":null,"position_opened_date":null,'
            '"degraded_from_action":null,'
            '"key_points":[],"evidence_ids":[],'
            '"capability_flags":{},"data_gaps":[]}'
        )

    def _validate_evidence(self, value: BaseModel) -> None:
        evidence_ids: list[str] = []
        findings = []
        if isinstance(value, RoleOutput):
            for finding in value.key_findings:
                evidence_ids.extend(finding.evidence_ids)
                findings.append(finding)
        elif isinstance(value, (DecisionPlan, TradeProposal, FinalDecision)):
            evidence_ids.extend(value.evidence_ids)
        try:
            self.evidence.validate_references(evidence_ids)
        except ValueError as exc:
            raise InvalidResponseError(str(exc)) from exc

        datasets_by_id = {
            item.evidence_id: item.dataset_id
            for item in self.evidence.all()
        }
        errors: list[str] = []
        for finding in findings:
            if not finding.evidence_ids:
                errors.append(f"Finding {finding.claim_id} must reference evidence")
                continue
            cited_datasets = {
                datasets_by_id[evidence_id]
                for evidence_id in finding.evidence_ids
                if evidence_id in datasets_by_id
            }
            missing_groups = [
                label
                for keywords, label, allowed in _EVIDENCE_CLAIM_RULES
                if finding.kind == "fact"
                and any(keyword.lower() in finding.claim.lower() for keyword in keywords)
                and not cited_datasets.intersection(allowed)
            ]
            if missing_groups:
                errors.append(
                    f"Finding {finding.claim_id} ({finding.claim[:80]}) "
                    "cites unrelated evidence for "
                    + ", ".join(missing_groups)
                    + "; cited datasets="
                    + ", ".join(sorted(cited_datasets))
                )
        if errors:
            raise InvalidResponseError("; ".join(errors))

    @staticmethod
    def _has_gaps(values: Any) -> bool:
        for value in values:
            if isinstance(value, dict) and value.get("data_gaps"):
                return True
        return False

    def _has_method_gaps(self) -> bool:
        return any(
            item["data_gaps"] or item["errors"]
            for item in self.method_executions.values()
        )

    @staticmethod
    def _markdown_for(role_id: str, value: BaseModel) -> str:
        data = value.model_dump()
        lines = [f"# {role_id}", ""]
        if "summary" in data:
            lines.extend([data["summary"], ""])
        if "rationale" in data:
            lines.extend([data["rationale"], ""])
        for finding in data.get("key_findings", []):
            lines.append(f"- **{finding['claim_id']}**: {finding['claim']}")
        return "\n".join(lines)
