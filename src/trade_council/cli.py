"""Trade-Council command line interface."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from . import __version__
from .config import ProjectConfig, load_project_config
from .data.snapshot import freeze as freeze_snapshot
from .data.snapshot import verify as verify_snapshot
from .decision.risk_rules import TradeContext, infer_board, validate_trade_context
from .engineering.datapack import build as build_datapack
from .engineering.datapack import norm_code
from .errors import TradeCouncilError
from .extensions.loader import discover_methods, load_resolved_methods
from .extensions.validators import validate_team_methods
from .llm.base import Message
from .llm.client import LLMClient
from .llm.providers import build_provider
from .logging_config import configure_logging
from .operations.runs import (
    cleanup_runs,
    export_report,
    list_runs,
    load_retention_policy,
    run_summary,
    summarize_calls,
)
from .orchestration.current_agent import prepare_current_agent_task
from .orchestration.finalization import finalize_run, load_finalization, validate_run
from .orchestration.workflow import WorkflowEngine
from .reporting.build import build_report


def _project_root(value: str) -> Path:
    return Path(value).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trade-council",
        description="Local, auditable, extensible multi-role investment research workbench.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    version_parser = subparsers.add_parser("version", help="Show the package version.")
    version_parser.set_defaults(handler=_version)

    config_parser = subparsers.add_parser("config", help="Inspect and validate configuration.")
    config_sub = config_parser.add_subparsers(dest="config_command")
    validate_parser = config_sub.add_parser("validate", help="Validate project configuration.")
    validate_parser.add_argument("--root", default=".", type=_project_root)
    validate_parser.set_defaults(handler=_config_validate)

    methods_parser = subparsers.add_parser("methods", help="Inspect method packages.")
    methods_sub = methods_parser.add_subparsers(dest="methods_command")
    list_parser = methods_sub.add_parser("list", help="List discovered methods.")
    list_parser.add_argument("--root", default=".", type=_project_root)
    list_parser.set_defaults(handler=_methods_list)

    data_parser = subparsers.add_parser("data", help="Build a local stock data pack.")
    data_parser.add_argument("code", help="Stock code, for example 600519.SH or 600519.")
    data_parser.add_argument("--asof", default=None, help="As-of date YYYY-MM-DD or YYYYMMDD.")
    data_parser.add_argument("--out", default=None, help="Output directory.")
    data_parser.add_argument("--name", default=None, help="Optional stock name hint.")
    data_parser.add_argument("--years", type=int, default=8)
    data_parser.add_argument("--include-news", action="store_true")
    data_parser.add_argument("--official-metrics", default=None,
                             help="官方扣非/信用减值补录 JSON（结构化，含来源与期间）")
    data_parser.add_argument("--root", default=".", type=_project_root)
    data_parser.set_defaults(handler=_data)

    models_parser = subparsers.add_parser("models", help="Inspect and exercise model profiles.")
    models_sub = models_parser.add_subparsers(dest="models_command")
    models_list = models_sub.add_parser("list", help="List model profiles and mappings.")
    models_list.add_argument("--root", default=".", type=_project_root)
    models_list.set_defaults(handler=_models_list)
    models_check = models_sub.add_parser("check", help="Preflight configured providers.")
    models_check.add_argument("--root", default=".", type=_project_root)
    models_check.set_defaults(handler=_models_check)
    models_route = models_sub.add_parser("route", help="Resolve the model for a role.")
    models_route.add_argument("--root", default=".", type=_project_root)
    models_route.add_argument("--profile", required=True)
    models_route.add_argument("--role", required=True)
    models_route.add_argument("--deep-tier", choices=["auto", "flash", "pro"], default=None)
    models_route.set_defaults(handler=_models_route)
    models_probe = models_sub.add_parser("probe", help="Run a small model generation.")
    models_probe.add_argument("--root", default=".", type=_project_root)
    models_probe.add_argument("--profile", required=True)
    models_probe.add_argument("--role", required=True)
    models_probe.add_argument("--deep-tier", choices=["auto", "flash", "pro"], default=None)
    models_probe.add_argument("--prompt", default='Return exactly {"ok": true} as JSON.')
    models_probe.add_argument("--json", action="store_true")
    models_probe.add_argument("--allow-cloud", action="store_true")
    models_probe.set_defaults(handler=_models_probe)

    analyze_parser = subparsers.add_parser("analyze", help="Run the multi-role workflow.")
    analyze_parser.add_argument("code")
    analyze_parser.add_argument("--asof", default=None)
    analyze_parser.add_argument("--profile", choices=["batch", "deep"], default=None)
    analyze_parser.add_argument("--mode", choices=["full", "quick", "data"], default="full")
    analyze_parser.add_argument("--deep-tier", choices=["auto", "flash", "pro"], default=None)
    analyze_parser.add_argument("--include-news", action="store_true")
    analyze_parser.add_argument("--allow-cloud", action="store_true")
    analyze_parser.add_argument("--allow-web", action="store_true")
    analyze_parser.add_argument("--years", type=int, default=8)
    analyze_parser.add_argument("--datapack", default=None)
    analyze_parser.add_argument("--official-metrics", default=None,
                                help="官方扣非/信用减值补录 JSON（结构化，含来源与期间）")
    analyze_parser.add_argument("--run-id", default=None)
    analyze_parser.add_argument("--run-kind", choices=["normal", "golden"], default="normal")
    analyze_parser.add_argument("--capital", type=float, default=None)
    analyze_parser.add_argument("--position-state", choices=["flat", "holding"], default=None)
    analyze_parser.add_argument("--position-shares", type=int, default=None)
    analyze_parser.add_argument("--position-opened-date", default=None)
    analyze_parser.add_argument("--board", choices=["main", "star", "bj"], default=None)
    analyze_parser.add_argument("--root", default=".", type=_project_root)
    analyze_parser.add_argument("--resume", action="store_true")
    analyze_parser.set_defaults(handler=_analyze)

    report_parser = subparsers.add_parser("report", help="Build Markdown/HTML report and audit.")
    report_parser.add_argument("--run-id", required=True)
    report_parser.add_argument("--datapack", default=None)
    report_parser.add_argument("--root", default=".", type=_project_root)
    report_parser.set_defaults(handler=_report)

    validate_parser = subparsers.add_parser(
        "validate", help="Check a run's artifacts, evidence and inputs without a model call."
    )
    validate_parser.add_argument("--run-id", required=True)
    validate_parser.add_argument("--root", default=".", type=_project_root)
    validate_parser.add_argument("--json", action="store_true")
    validate_parser.set_defaults(handler=_validate)

    finalize_parser = subparsers.add_parser(
        "finalize", help="Validate, run deterministic risk checks, build and seal the report."
    )
    finalize_parser.add_argument("--run-id", required=True)
    finalize_parser.add_argument("--root", default=".", type=_project_root)
    finalize_parser.add_argument("--json", action="store_true")
    finalize_parser.set_defaults(handler=_finalize)

    runs_parser = subparsers.add_parser("runs", help="Inspect runs, costs, and retention.")
    runs_sub = runs_parser.add_subparsers(dest="runs_command")
    runs_list = runs_sub.add_parser("list", help="List local runs.")
    runs_list.add_argument("--root", default=".", type=_project_root)
    runs_list.add_argument("--json", action="store_true")
    runs_list.set_defaults(handler=_runs_list)
    runs_inspect = runs_sub.add_parser("inspect", help="Inspect one run.")
    runs_inspect.add_argument("--run-id", required=True)
    runs_inspect.add_argument("--root", default=".", type=_project_root)
    runs_inspect.set_defaults(handler=_runs_inspect)
    runs_costs = runs_sub.add_parser("costs", help="Show token and cost summaries.")
    runs_costs.add_argument("--run-id", default=None)
    runs_costs.add_argument("--root", default=".", type=_project_root)
    runs_costs.add_argument("--json", action="store_true")
    runs_costs.set_defaults(handler=_runs_costs)
    runs_cleanup = runs_sub.add_parser("cleanup", help="Apply the retention policy.")
    runs_cleanup.add_argument("--root", default=".", type=_project_root)
    runs_cleanup.add_argument("--apply", action="store_true")
    runs_cleanup.set_defaults(handler=_runs_cleanup)

    export_parser = subparsers.add_parser("export", help="Export a report with a stable filename.")
    export_parser.add_argument("--run-id", required=True)
    export_parser.add_argument("--out-dir", default=None)
    export_parser.add_argument("--root", default=".", type=_project_root)
    export_parser.set_defaults(handler=_export)

    return parser


def _version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def _config_validate(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    configure_logging(config.app.log_level)
    registry = discover_methods(config.root / "methods")
    validate_team_methods(registry, config.team)
    print(
        json.dumps(
            {
                "ok": True,
                "root": str(config.root),
                "app": config.app.model_dump(),
                "roles": len(config.team.roles),
                "methods": len(registry.all()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _methods_list(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    methods = load_resolved_methods(config)
    for method in methods:
        state = "enabled" if method.enabled else "disabled"
        print(f"{method.manifest.id}\t{state}\t{method.manifest.domain}\t{method.manifest.type}")
    return 0


def _datapack_root(config: ProjectConfig, code6: str) -> Path:
    return config.root / config.app.run_dir / "datapacks" / code6


def _ensure_frozen(datapack: Path) -> dict[str, Any]:
    """Freeze a legacy/external pack in place, or verify an already frozen one."""

    manifest_path = datapack / "manifest.json"
    if not manifest_path.is_file():
        raise TradeCouncilError(f"数据包缺少 manifest.json：{datapack}")
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    if not manifest.get("content_hash") or not (datapack / "snapshot_files.json").is_file():
        frozen = freeze_snapshot(datapack)
        manifest["content_hash"] = frozen["content_hash"]
        manifest["hashes_file"] = "snapshot_files.json"
        manifest["hashed_files"] = len(frozen["files"])
        manifest["files"] = sorted(path.name for path in datapack.glob("*") if path.is_file())
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
    problems = verify_snapshot(datapack, str(manifest["content_hash"]))
    if problems:
        raise TradeCouncilError("数据包内容已变化，不能复用：" + "；".join(problems))
    return manifest


def _build_frozen_datapack(
    config: ProjectConfig,
    *,
    code: str,
    code6: str,
    asof: str | None,
    years: int,
    include_news: bool,
    name: str | None = None,
    official_metrics: Path | None = None,
) -> Path:
    """Build into a staging directory, then publish under the snapshot id."""

    root = _datapack_root(config, code6)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{os.getpid()}-{datetime.now().strftime('%H%M%S%f')}"
    try:
        manifest = build_datapack(
            code,
            asof,
            staging,
            years,
            name,
            include_news=include_news,
            data_root=Path(config.data.quant_data_root).expanduser(),
            official_metrics_path=official_metrics,
        )
        snapshot = str(manifest.get("snapshot_id") or "unknown")
        target = root / snapshot
        if target.exists():
            frozen = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if frozen.get("content_hash") and frozen["content_hash"] != manifest.get("content_hash"):
                raise TradeCouncilError(
                    f"同一 snapshot {snapshot} 已存在但内容不同；源数据可能已更新。"
                    "请确认后删除旧目录或换用新的 run，不要覆盖既有输入"
                )
            shutil.rmtree(staging, ignore_errors=True)
            return target
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _ensure_frozen(target)
    return target


def _adopt_datapack(config: ProjectConfig, source: Path, code6: str) -> Path:
    """Keep an external pack readable without modifying the user's files."""

    source = source.expanduser().resolve()
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise TradeCouncilError(f"外部数据包缺少 manifest.json：{source}")
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest.get("content_hash") and (source / "snapshot_files.json").is_file():
        problems = verify_snapshot(source, str(manifest["content_hash"]))
        if problems:
            raise TradeCouncilError("外部数据包内容已变化：" + "；".join(problems))
        return source
    snapshot = str(manifest.get("snapshot_id") or "imported")
    target = _datapack_root(config, code6) / f"{snapshot}-adopted"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    _ensure_frozen(target)
    return target


def _data(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    configure_logging(config.app.log_level)
    _, _, code6 = norm_code(args.code)
    if args.out:
        out = Path(args.out).expanduser().resolve()
        manifest = build_datapack(
            args.code,
            args.asof,
            out,
            args.years,
            args.name,
            include_news=args.include_news,
            data_root=Path(config.data.quant_data_root).expanduser(),
            official_metrics_path=Path(args.official_metrics) if args.official_metrics else None,
        )
    else:
        datapack = _build_frozen_datapack(
            config,
            code=args.code,
            code6=code6,
            asof=args.asof,
            years=args.years,
            include_news=args.include_news,
            name=args.name,
            official_metrics=Path(args.official_metrics) if args.official_metrics else None,
        )
        manifest = cast(dict[str, Any], json.loads((datapack / "manifest.json").read_text(encoding="utf-8")))
        manifest["data_pack_dir"] = str(datapack)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _models_list(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    if config.app.analysis_backend == "current_agent":
        print("backend=current_agent model=current_session; no external model API")
        print(f"batch_research_enabled={config.app.batch_research_enabled}")
        print("API profiles in models.yaml are legacy configuration, inactive for current-agent analysis.")
        return 0
    for name, profile in config.models.profiles.items():
        print(f"[{name}] {profile.display_name} provider={profile.provider}")
        for category, model in profile.models.items():
            print(f"  {category}: {model}")
        if profile.tier_models:
            print(f"  tier_models: {profile.tier_models}")
    return 0


def _models_check(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    if config.app.analysis_backend == "current_agent":
        print("current_agent\tOK\tUses the current chat agent; no API key or provider probe required.")
        return 0
    failures = 0
    for name, provider_config in config.models.providers.items():
        provider = build_provider(name, provider_config)
        status = asyncio.run(provider.preflight())
        print(f"{name}\t{'OK' if status.ok else 'FAIL'}\t{status.message}")
        if not status.ok:
            failures += 1
    return 1 if failures else 0


def _models_route(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    if config.app.analysis_backend == "current_agent":
        if args.profile == "batch" and not config.app.batch_research_enabled:
            print("error: batch research is paused", file=sys.stderr)
            return 2
        if args.role not in {role.id for role in config.team.roles}:
            print(f"error: unknown role: {args.role}", file=sys.stderr)
            return 2
        print(json.dumps({"profile": args.profile, "role": args.role,
                          "backend": "current_agent", "model": "current_session",
                          "external_api": False}, ensure_ascii=False, indent=2))
        return 0
    client = LLMClient(config.models)
    decision = client.route(
        role=args.role,
        profile=args.profile,
        deep_tier=args.deep_tier,
    )
    print(json.dumps(decision.__dict__, ensure_ascii=False, indent=2))
    return 0


def _models_probe(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    if config.app.analysis_backend == "current_agent":
        print("error: current-agent mode has no API probe; analysis runs in the current chat", file=sys.stderr)
        return 2
    if args.profile == "batch" and not config.app.batch_research_enabled:
        print("error: batch research is paused", file=sys.stderr)
        return 2
    provider_name = config.models.profiles[args.profile].provider
    provider_kind = config.models.providers[provider_name].kind
    if provider_kind != "ollama" and not args.allow_cloud:
        print("error: cloud probe requires --allow-cloud", file=sys.stderr)
        return 2
    client = LLMClient(config.models)
    messages = [Message(role="user", content=args.prompt)]
    if args.json:
        parsed, result, snapshot = asyncio.run(
            client.complete_json(
                role=args.role,
                profile=args.profile,
                messages=messages,
                deep_tier=args.deep_tier,
                max_tokens=128,
            )
        )
        print(
            json.dumps(
                {
                    "parsed": parsed,
                    "provider": result.provider,
                    "model": result.model,
                    "digest": result.model_digest,
                    "latency_ms": result.latency_ms,
                    "usage": result.usage.__dict__,
                    "snapshot": snapshot.__dict__,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        result = asyncio.run(
            client.complete(
                role=args.role,
                profile=args.profile,
                messages=messages,
                deep_tier=args.deep_tier,
                max_tokens=128,
            )
        )
        print(result.content)
    return 0


def _analyze(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    configure_logging(config.app.log_level)
    args.profile = args.profile or config.app.default_profile
    if args.profile == "batch" and not config.app.batch_research_enabled:
        print("error: batch research is paused; use single-stock --profile deep", file=sys.stderr)
        return 2
    if config.app.analysis_backend == "current_agent" and args.mode == "quick":
        print("error: quick research is paused in current-agent mode; use --mode full or data", file=sys.stderr)
        return 2
    if config.app.analysis_backend == "api":
        provider_name = config.models.profiles[args.profile].provider
        provider_kind = config.models.providers[provider_name].kind
        if provider_kind != "ollama" and not args.allow_cloud:
            print("error: cloud analysis requires --allow-cloud", file=sys.stderr)
            return 2

    _, _, code6 = norm_code(args.code)
    run_id = args.run_id or (
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{code6}-{args.profile}-{args.mode}"
    )
    run_dir = config.root / config.app.run_dir / run_id
    existing_path = run_dir / "run.json"
    existing = (
        json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.is_file() else {}
    )

    official_metrics = Path(args.official_metrics) if args.official_metrics else None
    if args.datapack:
        source = Path(args.datapack).expanduser().resolve()
        if not (source / "manifest.json").is_file():
            # Legacy behaviour: --datapack may point at a directory that does not
            # exist yet. Build it there, then adopt a frozen copy for the run.
            build_datapack(
                args.code,
                args.asof,
                source,
                args.years,
                include_news=args.include_news,
                data_root=Path(config.data.quant_data_root).expanduser(),
                official_metrics_path=official_metrics,
            )
        datapack = _adopt_datapack(config, source, code6)
    elif args.resume and existing.get("input_binding", {}).get("datapack_dir"):
        datapack = Path(str(existing["input_binding"]["datapack_dir"]))
        if not datapack.is_dir():
            print(f"error: frozen data pack is missing: {datapack}", file=sys.stderr)
            return 2
    else:
        datapack = _build_frozen_datapack(
            config,
            code=args.code,
            code6=code6,
            asof=args.asof,
            years=args.years,
            include_news=args.include_news,
            official_metrics=official_metrics,
        )

    trade_context = TradeContext(
        capital=args.capital,
        position_state=args.position_state,
        position_shares=args.position_shares,
        position_opened_date=args.position_opened_date,
        board=args.board or infer_board(args.code),
    )
    manifest = cast(dict[str, Any], json.loads((datapack / "manifest.json").read_text(encoding="utf-8")))
    context_errors = validate_trade_context(trade_context, str(manifest.get("asof", "")))
    if context_errors:
        for error in context_errors:
            print(f"error: {error}", file=sys.stderr)
        return 2

    run_dir.mkdir(parents=True, exist_ok=True)
    if config.app.analysis_backend == "current_agent":
        state = prepare_current_agent_task(
            config, datapack, run_dir, trade_context,
            mode=args.mode, include_news=args.include_news, allow_web=args.allow_web,
            run_kind=args.run_kind,
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))
        print("Read CURRENT_AGENT_TASK.md and complete analysis in the current chat; no model API was called.")
        return 0
    configure_logging(
        config.app.log_level,
        log_path=run_dir / "run.log",
        force=True,
    )
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=run_dir,
        profile=args.profile,
        mode=args.mode,
        deep_tier=args.deep_tier,
        include_news=args.include_news,
        trade_context=trade_context,
        run_kind=args.run_kind,
        web_search=args.allow_web,
        resume=args.resume,
    )
    state = asyncio.run(engine.run())
    if args.mode == "full" and state.get("status") == "DECIDED":
        audit = build_report(run_dir, datapack)
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        print(json.dumps(state, ensure_ascii=False, indent=2))
        print(json.dumps({"audit": audit}, ensure_ascii=False, indent=2))
        if audit["verdict"] == "FAIL":
            return 1
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _resolve_datapack(config: ProjectConfig, args: argparse.Namespace, run_dir: Path) -> Path:
    if args.datapack:
        return Path(args.datapack).expanduser().resolve()
    state_path = run_dir / "run.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        bound = (state.get("input_binding") or {}).get("datapack_dir") or state.get("data_pack_dir")
        if bound:
            return Path(str(bound))
    raise TradeCouncilError("无法确定数据包目录：请传 --datapack，或使用带 input_binding 的运行")


def _report(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    run_dir = config.root / config.app.run_dir / args.run_id
    state_path = run_dir / "run.json"
    if not state_path.is_file():
        print(f"error: run not found: {run_dir}", file=sys.stderr)
        return 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") in {"AWAITING_CURRENT_AGENT", "DATA_READY"}:
        print("error: analysis is not complete; cannot render a final report", file=sys.stderr)
        return 2
    # Rendering also re-checks the binding and artifacts: editing run.json by
    # hand must not be enough to bypass validation.
    report = validate_run(config, args.run_id)
    if not report.ok:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        print("error: validation failed; fix the items above before rendering", file=sys.stderr)
        return 2
    datapack = _resolve_datapack(config, args, run_dir)
    audit = build_report(run_dir, datapack)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["verdict"] != "FAIL" else 1


def _validate(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    report = validate_run(config, args.run_id)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"run {args.run_id}: {'PASS' if report.ok else 'FAIL'}")
        print(f"  已校验角色 {len(report.roles_checked)} 个，证据 {report.evidence_count} 条，"
              f"输入绑定 {'OK' if report.input_ok else '异常'}")
        for finding in report.findings:
            if finding.level == "INFO":
                continue
            print(f"  [{finding.level}] {finding.code}: {finding.message}"
                  + (f"（{finding.where}）" if finding.where else ""))
        if report.todos:
            print("  待办：")
            for todo in report.todos:
                print(f"   - {todo}")
    return 0 if report.ok else 1


def _finalize(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    record = finalize_run(config, args.run_id)
    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(f"run {args.run_id}: {'FINALIZED' if record['ok'] else 'NOT FINALIZED'}")
        print(f"  stage={record.get('stage')}  reason={record.get('reason', '—')}")
        if record.get("validation_ok") is False:
            for failure in record.get("failures", []):
                print(f"  [FAIL] {failure['code']}: {failure['message']}")
            for todo in record.get("todos", []):
                print(f"   - {todo}")
        if record.get("audit_verdict"):
            print(f"  audit={record['audit_verdict']}")
            for issue in record.get("audit_issues", []):
                if issue.get("level") == "FAIL":
                    print(f"  [FAIL] {issue['message']}")
    return 0 if record["ok"] else 1


def _runs_list(args: argparse.Namespace) -> int:
    summaries = list_runs(args.root / "runs")
    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0
    print("RUN_ID\tSTATUS\tKIND\tSYMBOL\tASOF\tPROFILE\tACTION\tAUDIT\tCALLS\tCOST")
    for item in summaries:
        cost = item["cost"]
        print(
            "\t".join(
                [
                    str(item["run_id"]),
                    str(item["status"]),
                    str(item["run_kind"]),
                    str(item["symbol"]),
                    str(item["asof"]),
                    str(item["profile"]),
                    str(item["final_action"] or "-"),
                    str(item["audit_verdict"] or "-"),
                    str(cost["calls"]),
                    str(cost["estimated_cost"]),
                ]
            )
        )
    return 0


def _runs_inspect(args: argparse.Namespace) -> int:
    summary = run_summary(args.root / "runs" / args.run_id)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _runs_costs(args: argparse.Namespace) -> int:
    if args.run_id:
        costs: Any = summarize_calls(args.root / "runs" / args.run_id)
        costs["run_id"] = args.run_id
    else:
        costs = [
            {"run_id": item["run_id"], **item["cost"]}
            for item in list_runs(args.root / "runs")
        ]
    if args.json:
        print(json.dumps(costs, ensure_ascii=False, indent=2))
        return 0
    rows = [costs] if isinstance(costs, dict) else costs
    print("RUN_ID\tCALLS\tPROMPT\tCOMPLETION\tTOTAL\tCOST\tCURRENCY\tUNKNOWN")
    for item in rows:
        print(
            "\t".join(
                [
                    str(item.get("run_id", "-")),
                    str(item.get("calls", 0)),
                    str(item.get("prompt_tokens", 0)),
                    str(item.get("completion_tokens", 0)),
                    str(item.get("total_tokens", 0)),
                    str(item.get("estimated_cost")),
                    ",".join(item.get("currencies", [])),
                    str(item.get("unknown_priced_calls", 0)),
                ]
            )
        )
    return 0


def _runs_cleanup(args: argparse.Namespace) -> int:
    policy = load_retention_policy(args.root / "configs" / "retention.yaml")
    result = cleanup_runs(args.root, policy, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _export(args: argparse.Namespace) -> int:
    config = load_project_config(args.root)
    run_dir = config.root / config.app.run_dir / args.run_id
    record = load_finalization(run_dir)
    if not record or not record.get("ok"):
        print(
            "error: run has not been finalized; run validate and finalize first",
            file=sys.stderr,
        )
        return 2
    report_path = run_dir / "report.html"
    if not report_path.is_file():
        print("error: finalized report is missing", file=sys.stderr)
        return 2
    actual = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if actual != record.get("report_html_sha256"):
        print(
            "error: report.html changed after finalize; re-run finalize instead of exporting",
            file=sys.stderr,
        )
        return 2
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    target = export_report(
        args.root,
        args.run_id,
        out_dir=out_dir,
        runs_root=config.root / config.app.run_dir,
    )
    print(str(target))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return int(handler(args))
    except TradeCouncilError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
