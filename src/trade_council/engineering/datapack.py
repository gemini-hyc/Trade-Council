#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_datapack.py — 个股分析数据包提取器（分析前置工序 Stage 0）

目的：把「先做数据工程、再动笔写报告」从习惯固化为脚本，消除两类高频错误：
  1. 把「我没找到」误写成「数据不存在」（数据集存在但无该标的记录 vs 数据集本身缺失，必须区分）
  2. 技术指标口径漂移（RSI 用简单均值 vs Wilder 平滑，同一只股票能差一倍以上）

用法：
  python stock_datapack.py --code 600487.SH
  python stock_datapack.py --code 003031.SZ --asof 2026-10-20 --out /tmp/dp
  python stock_datapack.py --code 600487 --name 亨通光电 --years 6

设计约束：
  - 数据源单一：本地 ~/quant-data（可用 QUANT_DATA 环境变量覆盖），不联网
  - 复权口径：adj_close = close * adj_factor[t] / adj_factor[asof]
  - 所有价格水平类指标（MA/BOLL/ATR/前高前低）一律基于复权价
  - 每个数据集都登记 status：OK / NO_RECORD / DATASET_EMPTY / DATASET_ABSENT / ERROR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.dataset as pads

from ..data.local_quant import LocalQuantProvider
from ..data.registry import DatasetRegistry
from .evidence import EvidenceIndex
from .events import append_event_digest, load_events, write_event_artifacts
from .financials import CALC_RULES_VERSION, build_financial_artifacts
from .indicators import MA_WINDOWS, add_indicators, ma_alignment, main_net_amount

BASE = Path(os.environ.get("QUANT_DATA", "~/quant-data")).expanduser()
PROVIDER = LocalQuantProvider(BASE)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = PROJECT_ROOT / "configs" / "datasets.yaml"
DATA_CONTRACT_PATH = BASE / "DATA_CONTRACT.md"

# ---------------------------------------------------------------- 检查器

class Registry:
    """数据集完备性登记表 —— 报告里每一句「数据缺口」都必须能在这里找到依据。"""

    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def add(self, key, label, status, rows=0, span="", note="", path=""):
        self.items.append(
            dict(key=key, label=label, status=status, rows=int(rows),
                 span=span, note=note, path=str(path))
        )

    def get(self, key):
        for it in self.items:
            if it["key"] == key:
                return it
        return None

    def has(self, key):
        it = self.get(key)
        return bool(it and it["status"] == "OK" and it["rows"] > 0)

    def status_of(self, key):
        it = self.get(key)
        return it["status"] if it else "NOT_CHECKED"


REG = Registry()


def norm_code(raw: str) -> tuple[str, str, str]:
    """归一化股票代码 -> (ts_code, dot_code, code6)"""
    s = str(raw).strip().upper().replace(" ", "")
    if "." in s:
        a, b = s.split(".")
        if a.isdigit():
            code6, ex = a, b
        else:
            ex, code6 = a, b
    else:
        code6 = s
        if code6.startswith(("6", "9")):
            ex = "SH"
        elif code6.startswith(("0", "3")):
            ex = "SZ"
        elif code6.startswith(("4", "8")):
            ex = "BJ"
        else:
            ex = "SH"
    code6 = code6.zfill(6)
    ex = ex.upper()
    ts = f"{code6}.{ex}"
    dot = f"{ex.lower()}.{code6}"
    return ts, dot, code6


def trade_dates() -> list[str]:
    return PROVIDER.trade_dates()


def scan(dirpath: Path, columns: list[str] | None, match: tuple[str, str] | None, label: str, key: str,
         since: int | None = None):
    """扫描目录并按等值条件取行。

    match = (字段名, 值)，用于构造过滤条件，同时供逐文件兜底复用。
    since = 只处理文件名年份 >= since 的文件（逐文件兜底模式下用于控制 IO）。
    目录之间/文件之间的 parquet schema 常有差异（同一列在不同文件里 dtype 不同，
    甚至整列为 null 型），pyarrow dataset 会因此报 cast 错误 —— 此时退回逐文件读取。
    返回 DataFrame；失败时返回 None 并登记原因。
    """
    d = dirpath
    if not d.exists():
        REG.add(key, label, "DATASET_ABSENT", 0, "", f"路径不存在：{d}", d)
        return None
    files = sorted(d.rglob("*.parquet"))
    if not files:
        REG.add(key, label, "DATASET_EMPTY", 0, "", "目录存在但无 parquet 文件", d)
        return None

    try:
        ds = pads.dataset(str(d), format="parquet")
        avail = set(ds.schema.names)
        cols = [c for c in columns if c in avail] if columns else None
        missing = [c for c in (columns or []) if c not in avail]
        flt = (pads.field(match[0]) == match[1]) if match else None
        tbl = ds.to_table(filter=flt, columns=cols)
        df = tbl.to_pandas()
        note = f"缺列：{missing}" if missing else ""
        if len(df) == 0:
            REG.add(key, label, "NO_RECORD", 0, "",
                    "数据集存在且有数据，但无该标的记录（不等于数据不存在）", d)
            return df
        REG.add(key, label, "OK", len(df), "", note, d)
        return df
    except Exception as e:  # noqa: BLE001
        print(f"[INFO] {label} dataset 扫描失败（{type(e).__name__}），转逐文件模式", file=sys.stderr)

    # ---- 逐文件兜底 ----
    frames = []
    errs = []
    if since:
        files = [f for f in files if not f.stem[:4].isdigit() or int(f.stem[:4]) >= since]
    for f in files:
        try:
            head = pd.read_parquet(f)
            if match and match[0] not in head.columns:
                continue
            take = [c for c in (columns or head.columns) if c in head.columns]
            sub = head[take]
            if match:
                sub = sub[sub[match[0]].astype(str) == str(match[1])]
            if len(sub):
                frames.append(sub)
        except Exception as ex:  # noqa: BLE001
            errs.append(f"{f.name}:{type(ex).__name__}")
            continue
    if not frames:
        REG.add(key, label, "NO_RECORD", 0, "",
                "逐文件模式未命中（若伴随读取错误需人工确认）" + (f"；错误 {len(errs)} 个" if errs else ""), d)
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    REG.add(key, label, "OK", len(df), "",
            "逐文件兜底模式（schema 不一致）" + (f"；{len(errs)} 个文件读取失败" if errs else ""), d)
    return df


def _validate_price_frame(px: pd.DataFrame, ts_code: str) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close", "volume", "amount"]
    missing = [column for column in required if column not in px.columns]
    if missing:
        sys.exit(f"[FATAL] {ts_code} 行情缺少关键列：{', '.join(missing)}")
    all_null = [column for column in required if px[column].isna().all()]
    if all_null:
        sys.exit(f"[FATAL] {ts_code} 行情关键列整列为空：{', '.join(all_null)}")
    null_counts = {
        column: int(px[column].isna().sum())
        for column in required
        if px[column].isna().any()
    }
    if null_counts:
        detail = ", ".join(f"{column}={count}" for column, count in null_counts.items())
        REG.add("price_quality", "行情完整性", "WARN", len(px), "",
                f"关键列存在空值：{detail}", "")
    px = px.dropna(subset=["date", "close"]).reset_index(drop=True)
    if len(px) < 2:
        sys.exit(f"[FATAL] {ts_code} 有效行情不足 2 行")
    return px


def _load_dataset_registry() -> DatasetRegistry:
    return DatasetRegistry.load(REGISTRY_PATH)


def _available_at(frame: pd.DataFrame, fields: tuple[str, ...]) -> pd.Series:
    for field in fields:
        if field in frame.columns:
            return pd.to_datetime(frame[field], errors="coerce")
    return pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")


def _filter_asof(
    frame: pd.DataFrame,
    asof_ts: pd.Timestamp,
    *,
    lag_days: int = 0,
    fields: tuple[str, ...] = (),
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    result = frame.copy()
    if fields:
        available = _available_at(result, fields)
        if lag_days:
            available = available + pd.to_timedelta(lag_days, unit="D")
        result["available_at"] = available
        result = result[result["available_at"] <= asof_ts]
    return result


def _snapshot_id(
    code6: str,
    asof: str,
    source_fingerprints: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
) -> str:
    h = hashlib.sha256()
    h.update(code6.encode())
    h.update(asof.encode())
    for item in source_fingerprints:
        h.update(json.dumps(item, sort_keys=True, ensure_ascii=False).encode())
    for item in datasets:
        h.update(json.dumps(item, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- 主流程

def build(
    code_raw: str,
    asof: str | None,
    out_dir: Path,
    years: int,
    name_hint=None,
    include_news: bool = False,
    data_root: Path | None = None,
    official_metrics_path: Path | None = None,
):
    global BASE, PROVIDER, DATA_CONTRACT_PATH, REG
    if data_root is not None:
        BASE = Path(data_root).expanduser().resolve()
        PROVIDER = LocalQuantProvider(BASE)
        DATA_CONTRACT_PATH = BASE / "DATA_CONTRACT.md"
    REG = Registry()
    ts_code, dot_code, code6 = norm_code(code_raw)
    registry = _load_dataset_registry()

    dates = trade_dates()
    if not dates:
        sys.exit(f"[FATAL] 未找到交易日数据：{BASE/'market/daily'}")
    if asof:
        asof = asof.replace("-", "")
        asof_fmt = f"{asof[:4]}-{asof[4:6]}-{asof[6:]}"
        if asof_fmt not in dates:
            prior = [d for d in dates if d <= asof_fmt]
            if not prior:
                sys.exit(f"[FATAL] {asof} 之前无交易日数据")
            print(f"[INFO] {asof_fmt} 非交易日，回退到 {prior[-1]}", file=sys.stderr)
            asof_fmt = prior[-1]
    else:
        asof_fmt = dates[-1]
    asof = asof_fmt
    asof_ts = pd.Timestamp(f"{asof} 23:59:59")
    start_year = max(int(asof[:4]) - years, 2005)

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] {ts_code}  asof={asof}  out={out_dir}", file=sys.stderr)

    # ---------- 1. 行情 ----------
    px = scan(BASE / "market" / "daily",
              ["date", "code", "open", "high", "low", "close", "preclose",
               "volume", "amount", "turn", "pctChg", "stock_name"],
              ("code", dot_code), "日线行情（未复权）", "price")
    if px is None or len(px) == 0:
        sys.exit(f"[FATAL] {ts_code} 无行情数据，无法继续。请确认代码是否正确。")

    px["date"] = pd.to_datetime(px["date"])
    px = px[px["date"] <= pd.Timestamp(asof)]
    px = px[px["date"].dt.year >= start_year].sort_values("date").reset_index(drop=True)
    px = _validate_price_frame(px, ts_code)
    stock_name = px["stock_name"].dropna().iloc[-1] if px["stock_name"].notna().any() else (name_hint or code6)

    # ---------- 2. 复权因子 ----------
    af = scan(BASE / "adj_factor", ["date", "code", "ts_code", "adj_factor"],
              ("code", dot_code), "复权因子", "adj_factor")
    af_flag = False
    if af is None or len(af) == 0:
        px["adj_factor"] = 1.0
        px["adj_close"] = px["close"]
        px["adj_high"] = px["high"]
        px["adj_low"] = px["low"]
        px["adj_open"] = px["open"]
        af_flag = True
        REG.add("adj_applied", "复权处理", "ERROR", len(px), "",
                "无复权因子，已退化为不复权：所有价格水平类指标的绝对价位不可信", "")
    else:
        af["date"] = pd.to_datetime(af["date"])
        af = af[af["date"] <= pd.Timestamp(asof)].sort_values("date")
        px = px.merge(af[["date", "adj_factor"]], on="date", how="left")
        px["adj_factor"] = px["adj_factor"].ffill()
        if px["adj_factor"].isna().any():
            px["adj_factor"] = px["adj_factor"].bfill()
            REG.add("adj_applied", "复权处理", "WARN", len(px), "",
                    "部分早期日期缺复权因子，已用最近值回填", "")
        base_af = float(px["adj_factor"].iloc[-1])
        px["adj_close"] = px["close"] * px["adj_factor"] / base_af
        px["adj_high"] = px["high"] * px["adj_factor"] / base_af
        px["adj_low"] = px["low"] * px["adj_factor"] / base_af
        px["adj_open"] = px["open"] * px["adj_factor"] / base_af
        REG.add("adj_applied", "复权处理", "OK", len(px), "",
                f"基准日 adj_factor={base_af:.6f}，adj_close=close*f/{base_af:.6f}", "")

    px = add_indicators(px)
    px.to_csv(out_dir / "price_daily.csv", index=False, encoding="utf-8")
    last = px.iloc[-1]

    # ---------- 3. 估值 ----------
    db = scan(BASE / "daily_basic",
              ["ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f",
               "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio",
               "dv_ttm", "total_share", "float_share", "free_share", "total_mv", "circ_mv"],
              ("ts_code", ts_code), "每日估值指标", "daily_basic")
    if db is not None and len(db):
        db["trade_date"] = db["trade_date"].astype(str)
        db = db[db["trade_date"] <= asof.replace("-", "")].sort_values("trade_date")
        db.to_csv(out_dir / "daily_basic.csv", index=False, encoding="utf-8")
        REG.get("daily_basic")["span"] = f'{db["trade_date"].iloc[0]} ~ {db["trade_date"].iloc[-1]}'
        last_db = db.iloc[-1]
        valuation_fields = ["pe_ttm", "pb", "ps_ttm"]
        empty_valuation = [
            field
            for field in valuation_fields
            if field in last_db and pd.isna(last_db.get(field))
        ]
        if len(empty_valuation) == len(valuation_fields):
            REG.add(
                "daily_basic_quality",
                "估值字段完整性",
                "ERROR",
                len(db),
                str(last_db.get("trade_date", "")),
                f"最新交易日估值字段整列为空：{', '.join(empty_valuation)}",
                BASE / "daily_basic",
            )
        else:
            REG.add(
                "daily_basic_quality",
                "估值字段完整性",
                "OK",
                len(db),
                str(last_db.get("trade_date", "")),
                f"缺失字段：{', '.join(empty_valuation) or '无'}",
                BASE / "daily_basic",
            )

    # ---------- 4. 资金流 ----------
    mf = scan(BASE / "moneyflow",
              ["ts_code", "trade_date", "buy_sm_vol", "buy_sm_amount", "sell_sm_vol",
               "sell_sm_amount", "buy_md_vol", "buy_md_amount", "sell_md_vol",
               "sell_md_amount", "buy_lg_vol", "buy_lg_amount", "sell_lg_vol",
               "sell_lg_amount", "buy_elg_vol", "buy_elg_amount", "sell_elg_vol",
               "sell_elg_amount", "net_mf_vol", "net_mf_amount"],
              ("ts_code", ts_code), "个股资金流", "moneyflow")
    if mf is not None and len(mf):
        mf["trade_date"] = mf["trade_date"].astype(str)
        mf = mf[mf["trade_date"] <= asof.replace("-", "")].sort_values("trade_date")
        mf.to_csv(out_dir / "moneyflow.csv", index=False, encoding="utf-8")
        REG.get("moneyflow")["span"] = f'{mf["trade_date"].iloc[0]} ~ {mf["trade_date"].iloc[-1]}'

    # ---------- 5. 情绪面 ----------
    md = scan(BASE / "sentiment" / "margin_detail",
              ["date", "code", "ts_code", "rzye", "rqye", "rzmre", "rqyl", "rzche",
               "rqchl", "rqmcl", "rzrqye"],
              ("ts_code", ts_code), "两融明细", "margin_detail")
    if md is not None and len(md):
        md["date"] = pd.to_datetime(md["date"])
        md = _filter_asof(md, asof_ts, lag_days=1, fields=("date",)).sort_values("date")
        if "rqye" in md and "rzrqye" in md:
            md["margin_rq_missing_n"] = md["rqye"].isna().astype(int)
            md["rzrqye"] = md["rzrqye"].fillna(md["rzye"] + md["rqye"].fillna(0))
        if len(md):
            md.to_csv(out_dir / "sent_margin_detail.csv", index=False, encoding="utf-8")
            REG.get("margin_detail")["span"] = f'{md["date"].iloc[0].date()} ~ {md["date"].iloc[-1].date()}'
            REG.get("margin_detail")["note"] = "T+1 发布；rzrqye 缺失已按 rzye+rqye.fillna(0) 重建"

    tl = scan(BASE / "sentiment" / "top_list",
              ["date", "code", "ts_code", "name", "close", "pct_change", "turnover_rate",
               "amount", "l_sell", "l_buy", "l_amount", "net_amount", "net_rate",
               "amount_rate", "float_values", "reason"],
              ("ts_code", ts_code), "龙虎榜", "top_list")
    if tl is not None and len(tl):
        tl["date"] = pd.to_datetime(tl["date"])
        tl = _filter_asof(tl, asof_ts, lag_days=1, fields=("date",)).sort_values("date")
        if len(tl):
            tl.to_csv(out_dir / "sent_top_list.csv", index=False, encoding="utf-8")
            REG.get("top_list")["span"] = f'{tl["date"].iloc[0].date()} ~ {tl["date"].iloc[-1].date()}'

    bt = scan(BASE / "sentiment" / "block_trade",
              ["date", "code", "ts_code", "price", "vol", "amount", "buyer", "seller"],
              ("ts_code", ts_code), "大宗交易", "block_trade")
    if bt is not None and len(bt):
        bt["date"] = pd.to_datetime(bt["date"])
        bt = _filter_asof(bt, asof_ts, lag_days=1, fields=("date",)).sort_values("date")
        if len(bt):
            bt.to_csv(out_dir / "sent_block_trade.csv", index=False, encoding="utf-8")
            REG.get("block_trade")["span"] = f'{bt["date"].iloc[0].date()} ~ {bt["date"].iloc[-1].date()}'
            REG.get("block_trade")["note"] = "仅保留该股记录；不跨证券类型求和"

    hs = scan(BASE / "sentiment" / "hsgt_top10",
              ["date", "code", "ts_code", "name", "close", "change", "rank",
               "market_type", "amount", "net_amount", "buy", "sell"],
              ("ts_code", ts_code), "北向前十大", "hsgt_top10")
    if hs is not None and len(hs):
        hs["date"] = pd.to_datetime(hs["date"])
        hs = _filter_asof(hs, asof_ts, lag_days=1, fields=("date",)).sort_values("date")
        if "net_amount" in hs:
            hs["northbound_net_amount_available"] = hs["date"] <= pd.Timestamp("2024-08-18")
            hs.loc[~hs["northbound_net_amount_available"], "net_amount"] = np.nan
        if len(hs):
            hs.to_csv(out_dir / "sent_hsgt_top10.csv", index=False, encoding="utf-8")
            REG.get("hsgt_top10")["span"] = f'{hs["date"].iloc[0].date()} ~ {hs["date"].iloc[-1].date()}'
            if (hs["date"] > pd.Timestamp("2024-08-18")).any():
                REG.get("hsgt_top10")["status"] = "WARN"
                REG.get("hsgt_top10")["note"] = "2024-08-19 后 net_amount 字段不可用；仅保留 amount"

    # ---------- 6. 财务三表 ----------
    # 利润表只取原库已有的列；缺列不假设存在，由 DataNeed 补录。
    INC_COLS = ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
                "basic_eps", "diluted_eps", "total_revenue", "revenue", "oper_cost",
                "biz_tax_surchg", "sell_exp", "admin_exp", "rd_exp", "fin_exp",
                "fv_value_chg_gain", "invest_income", "ass_invest_income",
                "assets_impair_loss", "operate_profit",
                "non_oper_income", "non_oper_exp", "total_profit",
                "income_tax", "n_income", "n_income_attr_p", "minority_gain"]
    BAL_COLS = ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
                "total_assets", "total_liab", "total_hldr_eqy_exc_min_int",
                "total_hldr_eqy_inc_min_int", "minority_int", "money_cap",
                "accounts_receiv", "notes_receiv", "accounts_receiv_bill", "inventories",
                "goodwill", "fix_assets", "cip", "intan_assets", "total_cur_assets",
                "total_cur_liab", "st_borr", "lt_borr", "bond_payable",
                "non_cur_liab_due_1y", "contract_assets", "contract_liab",
                "total_share", "st_fin_payable", "oth_cur_liab"]
    CF_COLS = ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
               "net_profit", "n_cashflow_act", "n_cashflow_inv_act",
               "n_cash_flows_fnc_act", "c_fr_sale_sg", "c_paid_goods_s",
               "c_paid_to_for_empl", "c_paid_for_taxes", "free_cashflow",
               "depr_fa_coga_dpba", "amort_intang_assets", "c_pay_acq_const_fiolta",
               "n_incr_cash_cash_equ"]

    fin: dict[str, Any] = {}
    for kind, cols, label in (("income", INC_COLS, "利润表"),
                              ("balancesheet", BAL_COLS, "资产负债表"),
                              ("cashflow", CF_COLS, "现金流量表")):
        d = scan(BASE / "financial_statements" / kind, cols,
                 ("ts_code", ts_code), f"{label}（累计口径）", kind)
        if d is None or len(d) == 0:
            fin[kind] = None
            continue
        d = d[d["report_type"].astype(str).isin(["1", "1.0"])] if "report_type" in d else d
        if len(d) == 0:
            fin[kind] = None
            REG.get(kind)["status"] = "NO_RECORD"
            REG.get(kind)["note"] = "仅有非合并报表（report_type!=1）"
            continue
        if "ann_date" not in d.columns:
            REG.get(kind)["status"] = "ERROR"
            REG.get(kind)["note"] = "缺少 ann_date，无法执行 as-of 过滤"
            fin[kind] = None
            continue
        available = pd.to_datetime(d["ann_date"], errors="coerce")
        if "f_ann_date" in d.columns:
            available = pd.to_datetime(d["f_ann_date"], errors="coerce").fillna(available)
        d["available_at"] = available
        d = d[d["available_at"] <= asof_ts]
        d = d.sort_values(["end_date", "available_at"]).drop_duplicates("end_date", keep="last")
        d = d.sort_values("end_date")
        if len(d) == 0:
            REG.get(kind)["status"] = "NO_RECORD"
            REG.get(kind)["note"] = "公告日均晚于 asof；不能使用未来披露的财报"
            fin[kind] = None
            continue
        d.to_csv(out_dir / f"{kind}.csv", index=False, encoding="utf-8")
        REG.get(kind)["span"] = f'{d["end_date"].iloc[0]} ~ {d["end_date"].iloc[-1]}'
        REG.get(kind)["note"] = "累计口径(YTD)，按公告日过滤；report_type=1 合并报表"
        fin[kind] = d

    # 合并财务宽表（保留原始三表；单季差分改由 financials 模块统一计算）
    fwide = None
    fin_artifacts: dict[str, Any] = {"status": "unavailable", "reason": "无财务三表输入"}
    try:
        parts = []
        for d in [fin["income"], fin["balancesheet"], fin["cashflow"]]:
            if d is None or not len(d):
                continue
            drop = [
                c
                for c in ("report_type", "ts_code", "ann_date", "f_ann_date", "available_at")
                if c in d.columns
            ]
            parts.append(d.drop(columns=drop))
        if parts:
            fwide = parts[0]
            for p in parts[1:]:
                fwide = fwide.merge(p, on="end_date", how="outer", suffixes=("", "_dup"))
            fwide = fwide.drop(columns=[c for c in fwide.columns if c.endswith("_dup")], errors="ignore")
            fwide = fwide.sort_values("end_date")
            fwide.to_csv(out_dir / "financials.csv", index=False, encoding="utf-8")
            REG.add("financials", "财务宽表（三表原始值）", "OK", len(fwide),
                    f'{fwide["end_date"].iloc[0]} ~ {fwide["end_date"].iloc[-1]}',
                    "单季差分改由 financials 模块按相邻季度键计算，见 financial_quarters.csv",
                    out_dir / "financials.csv")

        official_metrics = None
        if official_metrics_path is not None:
            try:
                official_metrics = json.loads(
                    Path(official_metrics_path).expanduser().read_text(encoding="utf-8")
                )
            except Exception as exc:  # noqa: BLE001
                REG.add("official_metrics", "官方扣非/信用减值补录", "ERROR", 0, "",
                        f"读取失败：{exc}", str(official_metrics_path))
        fin_artifacts = build_financial_artifacts(
            out_dir=out_dir,
            symbol=ts_code,
            asof=asof,
            income=fin["income"],
            cashflow=fin["cashflow"],
            balance=fin["balancesheet"],
            official_metrics=official_metrics,
        )
        if fin_artifacts.get("status") == "ok":
            checks = fin_artifacts["checks_summary"]
            REG.add(
                "financial_quarters",
                "季度财务与盈利桥接",
                "OK" if checks.get("fail", 0) == 0 else "WARN",
                len(fin_artifacts["series"].records),
                f'{fin_artifacts["series"].records[0].end_date} ~ '
                f'{fin_artifacts["series"].records[-1].end_date}' if fin_artifacts["series"].records else "",
                f'桥接对账 通过 {checks.get("pass", 0)}，失败 {checks.get("fail", 0)}，'
                f'不可得 {checks.get("unavailable", 0)}；计算规则 {CALC_RULES_VERSION}',
                out_dir / "earnings_bridge.json",
            )
            official_count = sum(
                1 for row in fin_artifacts["adjustments"]["periods"]
                if row.get("official_deducted_parent_net") is not None
            )
            REG.add(
                "official_deducted",
                "官方扣非归母",
                "OK" if official_count else "NO_RECORD",
                official_count,
                "",
                "来自公司公告补录；缺失即标缺失，不用未确认字段替代" if not official_count
                else f"{official_count} 期有官方扣非值",
                out_dir / "financial_adjustments.json",
            )
    except Exception as e:  # noqa: BLE001
        REG.add("financials", "财务宽表（三表合并）", "ERROR", 0, "", str(e), "")

    # ---------- 7. 业绩预告 / 快报（最高优先级催化，禁止跳过） ----------
    fc = None
    fdir = BASE / "reports" / "forecast"
    if fdir.exists():
        try:
            ds = pads.dataset(str(fdir), format="parquet")
            fc = ds.to_table(filter=pads.field("ts_code") == ts_code).to_pandas()
        except Exception as e:  # noqa: BLE001
            REG.add("forecast", "业绩预告", "ERROR", 0, "", str(e), fdir)
        if fc is not None:
            if len(fc) == 0:
                REG.add("forecast", "业绩预告", "NO_RECORD", 0, "",
                        "数据集存在但无该标的历史预告记录（可视为未发布过预告）", fdir)
            else:
                fc = fc[fc["ann_date"].astype(str) <= asof.replace("-", "")]
                if len(fc):
                    fc = fc.sort_values("ann_date")
                    fc.to_csv(out_dir / "forecast.csv", index=False, encoding="utf-8")
                    REG.add("forecast", "业绩预告", "OK", len(fc),
                            f'{fc["ann_date"].iloc[0]} ~ {fc["ann_date"].iloc[-1]}',
                            "关键催化数据，必须核实是否覆盖至最新", fdir)
                else:
                    REG.add("forecast", "业绩预告", "NO_RECORD", 0, "",
                            f"无 ann_date<={asof} 的记录", fdir)
    else:
        REG.add("forecast", "业绩预告", "DATASET_ABSENT", 0, "", "路径不存在", fdir)

    # 本地预告时效性检查
    fresh_gap = None
    if fdir.exists():
        fdates = sorted(p.stem for p in fdir.rglob("*.parquet"))
        if fdates:
            latest_local = fdates[-1]
            try:
                gap = (pd.Timestamp(asof) - pd.Timestamp(latest_local)).days
                fresh_gap = gap
                st = "OK" if gap <= 5 else "WARN"
                REG.add("forecast_freshness", "业绩预告数据时效（数据集级）", st, 0,
                        f"本地最新文件 {latest_local}",
                        (f"距基准日 {gap} 天，可直接使用" if gap <= 5 else
                         f"距基准日 {gap} 天，存在漏抓窗口：分析前必须用 Level 3 检索核实是否已有新预告"), fdir)
            except Exception:  # noqa: BLE001
                pass

    # 个股级预告时效 —— 数据集有数据不等于这只股票的预告是最新的
    # 中瓷电子（003031.SZ）2026-10-15 已披露三季报预告却未被发现，正是漏在这一层
    fc_gap = None
    if fc is not None and len(fc):
        try:
            last_ann = str(fc["ann_date"].max())
            fc_gap = (pd.Timestamp(asof) - pd.Timestamp(last_ann)).days
            ds_latest = sorted(p.stem for p in fdir.rglob("*.parquet"))[-1] if fdir.exists() else "—"
            mmdd = asof[5:7] + asof[8:10]
            in_window = ("0101" <= mmdd <= "0215" or "0401" <= mmdd <= "0515"
                         or "0701" <= mmdd <= "0831" or "1001" <= mmdd <= "1115")
            need_check = (in_window and fc_gap > 30) or fc_gap > 120
            st = "WARN" if need_check else "OK"
            if in_window:
                verdict = (f"基准日处于业绩预告披露窗口，而最新记录已距今 {fc_gap} 天 —— "
                           "**必须先 Level 3 核实是否已披露新预告，再动笔**"
                           if need_check else f"基准日处于预告窗口，记录距今 {fc_gap} 天，时效可接受")
            else:
                verdict = (f"非预告窗口期，记录距今 {fc_gap} 天" if not need_check
                           else f"记录距今 {fc_gap} 天，已超过一个财报季，需 Level 3 核实")
            REG.add("forecast_single", "业绩预告时效（个股级）", st, 0,
                    f"该股最新预告 {last_ann}（数据集文件已到 {ds_latest}）", verdict, fdir)
        except Exception:  # noqa: BLE001
            pass
    elif REG.status_of("forecast") == "NO_RECORD":
        REG.add("forecast_single", "业绩预告时效（个股级）", "WARN", 0, "无任何历史预告",
                "该股在本地预告库中零记录。临近财报季时，零记录本身就是需要 Level 3 核实的高危信号", fdir)

    ex = None
    edir = BASE / "reports" / "express"
    ex = scan(edir, None, ("ts_code", ts_code), "业绩快报", "express")
    if ex is not None and len(ex):
        ex = ex[ex["ann_date"].astype(str) <= asof.replace("-", "")].sort_values("ann_date")
        if len(ex):
            ex.to_csv(out_dir / "express.csv", index=False, encoding="utf-8")
            REG.get("express")["span"] = f'{ex["ann_date"].iloc[0]} ~ {ex["ann_date"].iloc[-1]}'
            REG.get("express")["note"] = "业绩快报为未经审计的年度/半年度快报，早于正式财报"
        else:
            REG.get("express")["status"] = "NO_RECORD"
            REG.get("express")["note"] = f"无 ann_date<={asof} 的记录"

    # ---------- 7.1 事件时间线：公告 / 快讯 / 业绩预告 ----------
    event_payload = load_events(
        BASE,
        code6=code6,
        ts_code=ts_code,
        asof=asof,
        include_news=include_news,
    )
    announcement_count = len(event_payload["announcements"])
    news_count = len(event_payload["news"])
    event_count = len(event_payload["events"])
    REG.add(
        "announcements_cninfo",
        "巨潮公告事件",
        "OK" if announcement_count else "NO_RECORD",
        announcement_count,
        "",
        "issuer 事件参与时间线；intermediary 文档保留在原始公告中",
        BASE / "announcements" / "cninfo",
    )
    if include_news:
        REG.add(
            "news_em_fast",
            "东财市场快讯",
            "OK" if news_count else "NO_RECORD",
            news_count,
            "",
            "a_codes 已按个股解析",
            BASE / "news" / "em_fast",
        )
    else:
        REG.add(
            "news_em_fast",
            "东财市场快讯",
            "DISABLED",
            0,
            "",
            "新闻默认关闭；仅 --include-news 时启用",
            BASE / "news" / "em_fast",
        )
    REG.add(
        "event_timeline",
        "标准化事件时间线",
        "OK" if event_count else "NO_RECORD",
        event_count,
        "",
        "公告、快讯和业绩预告统一事件视图",
        "",
    )

    # ---------- 8. 行业定位 ----------
    ind_info = None
    try:
        mpath = BASE / "industry" / "industry_members_daily.parquet"
        if mpath.exists():
            m = pd.read_parquet(mpath)
            m["date"] = pd.to_datetime(m["date"])
            m["stock_code"] = m["stock_code"].astype(str)
            # 行业成分表用 dot 格式（sh.600487），需与 ts_code 双向兼容
            sm = m[(m["stock_code"].isin([ts_code, dot_code])) & (m["date"] <= pd.Timestamp(asof))]
            if len(sm) == 0:
                REG.add("industry", "行业归属", "NO_RECORD", 0, "",
                        "industry_members_daily 中无该标的", mpath)
            else:
                last_d = sm["date"].max()
                sm = sm[sm["date"] == last_d]
                lst = pd.read_parquet(BASE / "industry" / "industry_list.parquet")
                ind_info = sm.merge(lst, left_on="ind_code", right_on="ts_code",
                                    how="left", suffixes=("", "_list"))
                ind_info["asof_membership"] = str(last_d.date())
                # 行业近期表现
                fpath = BASE / "industry" / "industry_daily_full.parquet"
                perf = None
                if fpath.exists():
                    fd = pd.read_parquet(fpath)
                    fd["date"] = pd.to_datetime(fd["date"])
                    fd = fd[fd["date"] <= pd.Timestamp(asof)]
                    codes = ind_info["ind_code"].unique().tolist()
                    sub = fd[fd["con_code"].isin(codes)].sort_values("date")
                    if len(sub):
                        latest = fd["date"].max()
                        day = fd[fd["date"] == latest].copy()
                        # 全行业当日排名
                        day["rank_1d"] = day["avg_pct"].rank(ascending=False)
                        # 近20日累计排名
                        cut = fd[fd["date"] > latest - pd.Timedelta(days=40)]
                        cum = cut.groupby("con_code")["avg_pct"].apply(lambda x: (1 + x / 100).prod() - 1)
                        cum = (cum * 100).rename("cum20_pct").reset_index()
                        cum["rank_20d"] = cum["cum20_pct"].rank(ascending=False)
                        ind_info = ind_info.merge(
                            day[["con_code", "avg_pct", "breadth", "n_stocks", "rank_1d"]],
                            left_on="ind_code", right_on="con_code", how="left")
                        ind_info = ind_info.merge(cum[["con_code", "cum20_pct", "rank_20d"]],
                                                  left_on="ind_code", right_on="con_code", how="left")
                        perf = len(day)
                if perf:
                    ind_info["total_industries"] = perf
                ind_info.drop(columns=[c for c in ("con_code_x", "con_code_y") if c in ind_info], inplace=True)
                ind_info.to_csv(out_dir / "industry.csv", index=False, encoding="utf-8")
                span = f'{sm["date"].min().date()} ~ {last_d.date()}'
                REG.add("industry", "行业归属与行业表现", "OK", len(ind_info), span,
                        "；".join(f'{r["name"]}={r["ind_code"]}' for _, r in ind_info.iterrows()),
                        mpath)
        else:
            REG.add("industry", "行业归属", "DATASET_ABSENT", 0, "", "路径不存在", mpath)
    except Exception as e:  # noqa: BLE001
        REG.add("industry", "行业归属与行业表现", "ERROR", 0, "", str(e), "")

    # ---------- 9. 新闻 / 公告 ----------
    if not include_news:
        REG.add(
            "news",
            "个股新闻",
            "DISABLED",
            0,
            "",
            "新闻默认关闭；仅 --include-news 时启用",
            BASE / "news",
        )
    else:
        try:
            ndir = BASE / "news"
            hits = []
            if ndir.exists():
                for f in sorted(ndir.glob("*.parquet")):
                    try:
                        d = pd.read_parquet(f)
                    except Exception:  # noqa: BLE001
                        continue
                    if "title" not in d.columns:
                        continue
                    mask = d["title"].astype(str).str.contains(stock_name, na=False) | \
                           d["title"].astype(str).str.contains(code6, na=False)
                    if mask.any():
                        hits.append(d[mask])
            if hits:
                nws = pd.concat(hits).drop_duplicates(subset=["title"] if "title" in hits[0] else None)
                time_field = next(
                    (field for field in ("publish_time", "pub_time", "publish_date") if field in nws.columns),
                    None,
                )
                if time_field:
                    nws[time_field] = pd.to_datetime(nws[time_field], errors="coerce")
                    nws["available_at"] = nws[time_field]
                    nws = nws[nws["available_at"] <= asof_ts]
                if len(nws):
                    nws.to_csv(out_dir / "news.csv", index=False, encoding="utf-8")
                    span = ""
                    if "available_at" in nws:
                        t = nws["available_at"].dropna()
                        if len(t):
                            span = f"{t.min().date()} ~ {t.max().date()}"
                    REG.add("news", "个股新闻", "OK", len(nws), span,
                            "新闻默认关闭；本次显式启用并已按 as-of 过滤", ndir)
                else:
                    REG.add("news", "个股新闻", "NO_RECORD", 0, "",
                            "启用新闻后，没有满足 as-of 的历史记录", ndir)
            else:
                REG.add("news", "个股新闻", "NO_RECORD", 0, "",
                        f"本地 news 目录存在但未匹配到「{stock_name}」/{code6} 的新闻", ndir)
        except Exception as e:  # noqa: BLE001
            REG.add("news", "个股新闻", "ERROR", 0, "", str(e), "")

    try:
        adir = BASE / "market"
        ahits = []
        ok_files = 0
        raw_files = 0
        for f in sorted(adir.glob("announcement_*.parquet")):
            try:
                d = pd.read_parquet(f)
            except Exception:  # noqa: BLE001
                continue
            if "title" not in d.columns:
                raw_files += 1          # 这类文件是资讯 API 的原始响应体，无结构化标题，无法按标的筛选
                continue
            ok_files += 1
            blob = d["title"].astype(str)
            for col in ("stock_infos", "summary"):
                if col in d.columns:
                    blob = blob + " " + d[col].astype(str)
            mask = blob.str.contains(stock_name, na=False) | blob.str.contains(code6, na=False)
            if mask.any():
                ahits.append(d[mask])
        if ahits:
            ann = pd.concat(ahits, ignore_index=True)
            keep = [c for c in ("publish_time", "publish_date", "title", "summary", "url", "source_original") if c in ann.columns]
            ann = ann[keep].drop_duplicates(subset=["title"] if "title" in keep else None)
            time_field = next(
                (field for field in ("publish_time", "publish_date") if field in ann.columns),
                None,
            )
            if time_field:
                ann[time_field] = pd.to_datetime(ann[time_field], errors="coerce")
                ann["available_at"] = ann[time_field]
                ann = ann[ann["available_at"] <= asof_ts]
            if "publish_date" in ann:
                ann = ann.sort_values("publish_date", ascending=False)
            if len(ann):
                ann.to_csv(out_dir / "announcements.csv", index=False, encoding="utf-8")
                REG.add("announcements", "公司公告", "OK", len(ann),
                        f'{ann["publish_date"].min()} ~ {ann["publish_date"].max()}' if "publish_date" in ann else "",
                        "公告可用；已按 publish_time/date 截断到 as-of", adir)
            else:
                REG.add("announcements", "公司公告", "NO_RECORD", 0, "",
                        "有匹配公告，但均晚于 as-of", adir)
        else:
            REG.add("announcements", "公司公告", "NO_RECORD", 0, "",
                    f"可解析公告文件 {ok_files} 个（另有 {raw_files} 个为 API 原始响应体、无标题列，无法筛选），"
                    "未匹配到该标的 —— 这是「本地样本无记录」，不等于公司无公告", adir)
    except Exception as e:  # noqa: BLE001
        REG.add("announcements", "公司公告", "ERROR", 0, "", str(e), "")

    # ---------- 10. 股东行为 ----------
    try:
        sdir = BASE / "stock" / ts_code
        got = []
        for fn in ("stk_holdertrade", "share_float", "pledge_stat", "namechange"):
            p = sdir / f"{fn}.parquet"
            if p.exists():
                d = pd.read_parquet(p)
                if len(d):
                    d.to_csv(out_dir / f"{fn}.csv", index=False, encoding="utf-8")
                    got.append(f"{fn}({len(d)})")
        if got:
            REG.add("holder", "股东行为（增减持/解禁/质押/更名）", "OK", len(got), "", "、".join(got), sdir)
        else:
            REG.add("holder", "股东行为（增减持/解禁/质押/更名）", "NO_RECORD", 0, "",
                    f"{sdir} 存在但无相关文件" if sdir.exists() else "该标的无个股级目录", sdir)
    except Exception as e:  # noqa: BLE001
        REG.add("holder", "股东行为", "ERROR", 0, "", str(e), "")

    # ---------- 11. 涨跌停 / 停牌 ----------
    for key, sub, label in (("stk_limit", "stk_limit", "涨跌停价格"),
                            ("suspend_d", "suspend_d", "停牌记录")):
        d = scan(BASE / sub,
                 ["date", "ts_code", "up_limit", "down_limit"],
                 ("ts_code", ts_code), label, key, since=int(asof[:4]) - 3)
        if d is not None and len(d):
            d["date"] = pd.to_datetime(d["date"], errors="coerce")
            d = d[d["date"] <= asof_ts].sort_values("date")
            if len(d):
                d.to_csv(out_dir / f"{key}.csv", index=False, encoding="utf-8")

    # ---------- 12. DATA_DIGEST.md ----------
    write_digest(out_dir, ts_code, code6, stock_name, asof, px, last, fwide, fc, ind_info,
                 fresh_gap, af_flag, fc_gap, db, include_news=include_news,
                 fin_artifacts=fin_artifacts)

    # ---------- 13. manifest.json ----------
    source_fingerprints = []
    for spec in registry.enabled():
        files = PROVIDER.dataset_files(spec)
        source_fingerprints.append(
            {
                "dataset_id": spec.id,
                "files": [
                    {
                        "path": str(path.relative_to(BASE)),
                        "size": path.stat().st_size,
                        "mtime_ns": path.stat().st_mtime_ns,
                    }
                    for path in files
                ],
            }
        )
    snapshot_id = _snapshot_id(code6, asof, source_fingerprints, REG.items)
    (out_dir / "snapshot.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "asof": asof,
                "asof_cutoff": f"{asof} 23:59:59",
                "timezone": "Asia/Shanghai",
                "datasets": source_fingerprints,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for event in event_payload["events"]:
        event["snapshot_id"] = snapshot_id
    write_event_artifacts(out_dir, event_payload)
    append_event_digest(out_dir, event_payload)

    evidence = EvidenceIndex(snapshot_id)
    price_date = str(last["date"].date())
    for field, value, unit, formula in (
        ("close", float(last["close"]), "CNY", "未复权收盘价"),
        (
            "adj_close",
            float(last["adj_close"]),
            "CNY",
            "close * adj_factor / adj_factor_asof",
        ),
        ("rsi14", float(last["rsi14"]), "index", "Wilder RSI(14) on adj_close"),
        ("rsi14_sma", float(last["rsi14_sma"]), "index", "SMA RSI(14) on adj_close"),
        ("atr14", float(last["atr14"]), "CNY", "EWM true range(14) on adjusted OHLC"),
        ("ma20", float(last["ma20"]), "CNY", "20-day SMA on adj_close"),
        ("ma60", float(last["ma60"]), "CNY", "60-day SMA on adj_close"),
        ("ret_20d", float(last["ret_20d"]), "ratio", "20-day return on adj_close"),
    ):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        evidence.add(
            dataset_id="market.daily",
            path="market/daily",
            primary_key=ts_code,
            event_date=price_date,
            available_at=f"{price_date} 15:00:00",
            field=field,
            value=value,
            unit=unit,
            formula=formula,
            source_hash=snapshot_id,
        )

    if db is not None and len(db):
        db_row = db.iloc[-1]
        db_date = str(db_row["trade_date"])
        for field, unit in (("pe_ttm", "x"), ("pb", "x"), ("ps_ttm", "x")):
            if field in db_row and pd.notna(db_row[field]):
                evidence.add(
                    dataset_id="market.daily_basic",
                    path="daily_basic",
                    primary_key=ts_code,
                    event_date=db_date,
                    available_at=db_date,
                    field=field,
                    value=float(db_row[field]),
                    unit=unit,
                    source_hash=snapshot_id,
                )

    if mf is not None and len(mf):
        main_net = main_net_amount(mf)
        for window in (1, 5, 20):
            value = float(main_net.tail(window).sum() / 1e4)
            evidence.add(
                dataset_id="market.moneyflow",
                path="moneyflow",
                primary_key=ts_code,
                event_date=asof,
                available_at=f"{asof} 23:59:59",
                field=f"main_net_{window}d",
                value=value,
                unit="亿元",
                formula="(buy_elg+buy_lg)-(sell_elg+sell_lg), sum window, 万元→亿元",
                source_hash=snapshot_id,
            )

    if md is not None and len(md):
        md_row = md.iloc[-1]
        evidence.add(
            dataset_id="sentiment.margin_detail",
            path="sentiment/margin_detail",
            primary_key=ts_code,
            event_date=str(pd.Timestamp(md_row["date"]).date()),
            available_at=str(pd.Timestamp(md_row["available_at"]).date()),
            field="rzrqye",
            value=float(md_row["rzrqye"]),
            unit="CNY",
            formula="rzrqye; missing rqye uses rzye+rqye.fillna(0)",
            source_hash=snapshot_id,
        )

    if tl is not None and len(tl):
        top_row = tl.iloc[-1]
        top_date = str(pd.Timestamp(top_row["date"]).date())
        if pd.notna(top_row.get("net_amount", np.nan)):
            evidence.add(
                dataset_id="sentiment.top_list",
                path="sentiment/top_list",
                primary_key=ts_code,
                event_date=top_date,
                available_at=f"{top_date} 23:59:59",
                field="net_amount",
                value=float(top_row["net_amount"]),
                unit="CNY",
                formula="latest top-list net_amount",
                source_hash=snapshot_id,
            )
        cutoff = pd.Timestamp(asof) - pd.Timedelta(days=365)
        recent_top = tl[tl["date"] >= cutoff]
        evidence.add(
            dataset_id="sentiment.top_list",
            path="sentiment/top_list",
            primary_key=ts_code,
            event_date=top_date,
            available_at=f"{top_date} 23:59:59",
            field="count_1y",
            value=int(len(recent_top)),
            unit="count",
            formula="top-list rows in trailing 365 calendar days",
            source_hash=snapshot_id,
        )

    if bt is not None and len(bt):
        block_recent = bt[bt["date"] >= pd.Timestamp(asof) - pd.Timedelta(days=90)]
        if len(block_recent):
            block_date = str(pd.Timestamp(block_recent["date"].iloc[-1]).date())
            evidence.add(
                dataset_id="sentiment.block_trade",
                path="sentiment/block_trade",
                primary_key=ts_code,
                event_date=block_date,
                available_at=f"{block_date} 23:59:59",
                field="amount_90d",
                value=float(block_recent["amount"].sum()) * 10_000,
                unit="CNY",
                formula="sum block-trade amount (万元→元) in trailing 90 calendar days",
                source_hash=snapshot_id,
            )

    if hs is not None and len(hs):
        hs_row = hs.iloc[-1]
        hs_date = str(pd.Timestamp(hs_row["date"]).date())
        evidence.add(
            dataset_id="sentiment.hsgt_top10",
            path="sentiment/hsgt_top10",
            primary_key=ts_code,
            event_date=hs_date,
            available_at=f"{hs_date} 23:59:59",
            field="amount",
            value=float(hs_row["amount"]),
            unit="CNY",
            formula="latest valid northbound top-10 amount",
            source_hash=snapshot_id,
            note="net_amount unavailable after 2024-08-18",
        )

    if fwide is not None and len(fwide):
        financial_row = fwide.iloc[-1]
        financial_date = str(financial_row["end_date"])
        financial_available = str(financial_row.get("available_at", f"{asof} 23:59:59"))
        for field, unit in (
            ("revenue", "CNY"),
            ("n_income_attr_p", "CNY"),
            ("n_cashflow_act", "CNY"),
        ):
            if field in financial_row and pd.notna(financial_row[field]):
                evidence.add(
                    dataset_id="financial.statements",
                    path="financial_statements",
                    primary_key=ts_code,
                    event_date=financial_date,
                    available_at=financial_available,
                    field=field,
                    value=float(financial_row[field]),
                    unit=unit,
                    formula="report_type=1; YTD; filtered by ann_date/f_ann_date",
                    source_hash=snapshot_id,
                    period=f"YTD to {financial_date}",
                    basis="cumulative_ytd",
                    value_type="number",
                )

    # 季度单季证据：只登记可得的单季值，并写明推导状态与可得时间。
    if fin_artifacts.get("status") == "ok":
        series = fin_artifacts["series"]
        for record in series.records[-6:]:
            for field in ("revenue", "oper_cost", "n_income_attr_p", "total_profit", "assets_impair_loss"):
                cell = record.values.get(field)
                if cell is None or cell.status != "ok" or cell.single is None:
                    continue
                evidence.add(
                    dataset_id="financial.quarters",
                    path="financial_quarters.csv",
                    primary_key=ts_code,
                    event_date=record.end_date,
                    available_at=record.available_at or f"{asof} 23:59:59",
                    field=f"{field}_single_q",
                    value=float(cell.single),
                    unit="CNY",
                    formula=(
                        "Q1 单季=Q1 累计；其余=本期累计−同年度上一季累计"
                        f"（前驱 {cell.predecessor or '—'}）"
                    ),
                    source_hash=snapshot_id,
                    calc_node="financials.quarterize",
                    period=f"{record.year}Q{record.quarter}",
                    basis="single_quarter",
                    value_type="number",
                    note="单季推导值；不是原披露累计数",
                )
        adv = fin_artifacts.get("adjustments", {})
        available_by_end = {
            record.end_date: record.available_at for record in series.records
        }
        for row in adv.get("periods", []):
            if row.get("official_deducted_parent_net") is None:
                continue
            evidence.add(
                dataset_id="financial.official",
                path="financial_adjustments.json",
                primary_key=ts_code,
                event_date=row["end_date"],
                available_at=available_by_end.get(row["end_date"]) or f"{asof} 23:59:59",
                field="official_deducted_parent_net",
                value=float(row["official_deducted_parent_net"]),
                unit="CNY",
                formula="公司公告披露的扣除非经常性损益后的归母净利润（累计口径，需按季差分）",
                source_hash=snapshot_id,
                source_level="web",
                source_url=row.get("official_source_url"),
                value_type="number",
                period=f"YTD to {row['end_date']}",
                basis="official_deducted_cumulative",
                note=str(row.get("official_location") or ""),
            )

    for event in event_payload["events"]:
        event["snapshot_id"] = snapshot_id
        evidence.add(
            dataset_id=event["source_dataset"],
            path=event["source_dataset"].replace(".", "/"),
            primary_key=ts_code,
            event_date=str(event["event_date"]),
            available_at=str(event["available_at"]),
            field="event",
            value=event["title"],
            unit="",
            formula=f"event_type={event['event_type']}",
            source_hash=snapshot_id,
            note=f"{event['event_id']} {event['summary'][:160]}",
            source_level=str(event["source_level"]),
            source_url=event["source_url"],
            extraction_confidence=str(event["confidence"]),
        )

    evidence.write(out_dir)
    evidence.append_digest(out_dir / "DATA_DIGEST.md")

    manifest = dict(
        ts_code=ts_code, code6=code6, stock_name=stock_name, asof=asof,
        asof_cutoff=f"{asof} 23:59:59", timezone="Asia/Shanghai",
        snapshot_id=snapshot_id,
        snapshot_file="snapshot.json",
        evidence_file="evidence.json",
        evidence_trace_file="evidence_trace.jsonl",
        evidence_count=len(evidence.all()),
        event_count=len(event_payload["events"]),
        announcement_count=len(event_payload["announcements"]),
        news_count=len(event_payload["news"]),
        events_file="events.csv",
        event_timeline_file="event_timeline.json",
        data_contract=str(DATA_CONTRACT_PATH),
        registry_version=registry.config.version,
        provider="local_quant",
        news_included=include_news,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        quant_data=str(BASE), years=years,
        adj_base_factor=float(px["adj_factor"].iloc[-1]),
        price_basis={
            "close": "raw_unadjusted",
            "adj_close": "qfq_base_asof",
            "adj_formula": "adj_close = close * adj_factor[t] / adj_factor[asof]",
            "adj_base_date": asof,
            "note": ("复权价只用于均线、ATR、支撑压力等水平类指标；"
                     "真实委托价必须用未复权价，两者不可混入同一收益路径"),
        },
        calc_rules_version=CALC_RULES_VERSION,
        financial_artifacts={
            "quarters": "financial_quarters.csv" if (out_dir / "financial_quarters.csv").exists() else None,
            "bridge": "earnings_bridge.json" if (out_dir / "earnings_bridge.json").exists() else None,
            "adjustments": ("financial_adjustments.json"
                            if (out_dir / "financial_adjustments.json").exists() else None),
            "checks_summary": fin_artifacts.get("checks_summary"),
        },
        bars=len(px), datasets=REG.items,
        files=sorted(p.name for p in out_dir.glob("*") if p.is_file()),
    )
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 控制台摘要 ----------
    bad = [it for it in REG.items if it["status"] not in ("OK",)]
    print(f"\n{'='*66}")
    print(f"  {stock_name} ({ts_code})  asof {asof}")
    print(f"{'='*66}")
    print(f"  行情 {len(px)} 根K线  最新收盘 {last['close']:.2f}  "
          f"复权 {last['adj_close']:.2f}  ATR14 {last['atr14']:.2f}({last['atr_pct']:.1f}%)")
    print(f"  输出目录 {out_dir}")
    print(f"  数据集 {len(REG.items)} 项，异常/缺口 {len(bad)} 项")
    for it in bad:
        print(f"    [{it['status']:14s}] {it['label']}: {it['note'][:80]}")
    print(f"{'='*66}\n")
    return manifest


def fmt(v, nd=2, unit=""):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "—"
        return f"{v:,.{nd}f}{unit}"
    except Exception:  # noqa: BLE001
        return str(v)


def pct(v, nd=2):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "—"
        return f"{v*100:+.{nd}f}%"
    except Exception:  # noqa: BLE001
        return str(v)


def write_digest(out_dir, ts_code, code6, stock_name, asof, px, last, fwide, fc,
                 ind_info, fresh_gap, af_flag, fc_gap=None, db=None, include_news=False,
                 fin_artifacts=None):
    L: list[str] = []
    A = L.append
    A(f"# 数据摘要 · {stock_name}（{ts_code}）")
    A("")
    A(f"- 基准日（asof）：**{asof}**")
    A(f"- as-of 截止时间：**{asof} 23:59:59（Asia/Shanghai）**")
    A(f"- 新闻分析：**{'启用' if include_news else '默认关闭'}**")
    A(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 数据源：本地 `{BASE}`（无联网）")
    A(f"- K线跨度：{px['date'].iloc[0].date()} ~ {px['date'].iloc[-1].date()}（{len(px)} 根）")
    if af_flag:
        A("- **复权异常：无复权因子，价格水平类指标不可信**")
    A("")

    # —— 数据完备性矩阵（最重要）——
    A("## 1. 数据完备性矩阵")
    A("")
    A("> 写报告时，任何一处「数据缺口」表述都必须与此表一致。")
    A("> `NO_RECORD` = 数据集存在但无该标的记录；`DATASET_ABSENT` = 本地根本没有这个数据集。")
    A("> **两者不可混为一谈**，更不得把「没找到」写成「数据不存在」。")
    A("")
    A("| 数据集 | 状态 | 行数 | 覆盖期 | 备注 |")
    A("|---|---|---|---|---|")
    flag = {"OK": "OK", "NO_RECORD": "NO_RECORD", "DATASET_ABSENT": "ABSENT",
            "DATASET_EMPTY": "EMPTY", "ERROR": "ERROR", "WARN": "WARN"}
    for it in REG.items:
        note = (it["note"] or "").replace("|", "/")[:90]
        A(f"| {it['label']} | {flag.get(it['status'], it['status'])} | {it['rows']} | "
          f"{it['span'] or '—'} | {note} |")
    A("")

    A("## 2. 最新交易日快照")
    A("")
    A("> **价格口径**：`收盘（未复权）` 是原始成交价，用于真实委托价格；"
      "`复权收盘` 是前复权价（基准日 = 本次 asof），只用于均线、ATR、支撑压力等水平类指标。"
      "两种口径不可相减、不可混在同一收益路径里。")
    A("")
    A("| 字段 | 值 | 字段 | 值 |")
    A("|---|---|---|---|")
    A(f"| 日期 | {last['date'].date()} | 收盘（未复权） | {fmt(last['close'])} |")
    A(f"| 涨跌幅 | {fmt(last['pctChg'])}% | 复权收盘 | {fmt(last['adj_close'])} |")
    A(f"| 成交量（万股） | {fmt(last['volume']/1e4, 0)} | 成交额（亿元） | {fmt(last['amount']/1e5)} |")
    A(f"| 换手率 | {fmt(last['turn'])}% | 量比(vs20日) | {fmt(last['vol_ratio20'])} |")
    A(f"| 复权因子 | {fmt(last['adj_factor'], 4)} | ATR14 | {fmt(last['atr14'])}（{fmt(last['atr_pct'])}%） |")
    A("")

    # —— 估值与历史分位 ——
    A("## 3. 估值与历史分位")
    A("")
    if db is not None and len(db):
        def q(series, v):
            s = pd.to_numeric(series, errors="coerce").dropna()
            if not len(s) or pd.isna(v):
                return None
            return float((s <= v).mean() * 100)

        cur = db.iloc[-1]
        n = len(db)
        A(f"分位基于本地 {n} 个交易日历史（{db['trade_date'].iloc[0]} ~ {db['trade_date'].iloc[-1]}）。")
        A("**估值不是只看绝对值，必须看历史分位。**")
        A("")
        A("| 指标 | 当前值 | 历史分位 | 判读 |")
        A("|---|---|---|---|")
        for col, lab in (("pe_ttm", "PE(TTM)"), ("pb", "PB"), ("ps_ttm", "PS(TTM)"),
                         ("dv_ttm", "股息率(TTM,%)")):
            if col in db:
                v = pd.to_numeric(cur.get(col), errors="coerce")
                p = q(db[col], v)
                if p is None:
                    A(f"| {lab} | — | — | 无有效值（可能亏损或数据缺失） |")
                else:
                    judge = ("极高分位（>90%），估值处于历史顶部区域" if p > 90 else
                             "偏高（75-90%分位）" if p > 75 else
                             "中性偏高（50-75%分位）" if p > 50 else
                             "中性偏低（25-50%分位）" if p > 25 else
                             "低位（<25%分位）")
                    if col == "dv_ttm":
                        judge = "股息率越高越好，分位越高代表相对便宜" if p > 50 else "股息率处于历史偏低水平"
                    A(f"| {lab} | {fmt(v)} | {fmt(p,1)}% | {judge} |")
        A("")
        for col, lab in (("total_mv", "总市值"), ("circ_mv", "流通市值")):
            if col in db and pd.notna(cur.get(col)):
                A(f"- {lab}：{fmt(float(cur[col])/1e4)} 亿元")
        A("")
        # 前瞻估值提示
        A("> 前瞻 PE 需把当前市值除以一致预期净利；本脚本不提供盈利预测，"
          "若使用卖方一致预期，必须在报告中标注预测方与发布日期。")
    else:
        A("_无估值数据（daily_basic 无该标的记录）_")
    A("")

    A("## 4. 均线体系（基于复权价）")
    A("")
    A("> 表中与下文所有均线数值均为**前复权价**（基准日 = 本次 asof），"
      "不等于当日可成交的原始价格。")
    A("")
    A("| 均线 | 数值 | 现价距离 | 均线 | 数值 | 现价距离 |")
    A("|---|---|---|---|---|---|")
    pairs = [("ma5", "ma10"), ("ma20", "ma30"), ("ma60", "ma120"), ("ma250", "boll_mid")]
    for a, b in pairs:
        lname = {"ma5": "MA5", "ma20": "MA20", "ma60": "MA60", "ma250": "MA250",
                 "ma10": "MA10", "ma30": "MA30", "ma120": "MA120", "boll_mid": "BOLL中轨"}[a]
        rname = {"ma10": "MA10", "ma30": "MA30", "ma120": "MA120", "boll_mid": "BOLL中轨"}[b]
        da = pct(last.get(f"dist_{a}")) if a.startswith("ma") else pct(last["adj_close"] / last[b] - 1)
        db = pct(last.get(f"dist_{b}")) if b.startswith("ma") else pct(last["adj_close"] / last[b] - 1)
        A(f"| {lname} | {fmt(last.get(a))} | {da} | {rname} | {fmt(last.get(b))} | {db} |")
    A("")
    arr = [last.get(f"ma{n}") for n in MA_WINDOWS]
    alignment = ma_alignment(arr)
    short_alignment = ma_alignment(arr[:3], MA_WINDOWS[:3], name="短期均线")
    mid_alignment = ma_alignment(arr[2:6], MA_WINDOWS[2:6], name="中期均线")
    A("均线排列判定（**逐对相邻严格比较**，相等或缺值不做方向推断）：")
    A("")
    A(f"- 全序列：**{alignment.label}**。{alignment.detail}")
    A(f"- 短期（MA5/MA10/MA20）：{short_alignment.label}。{short_alignment.detail}")
    A(f"- 中期（MA20/MA30/MA60/MA120）：{mid_alignment.label}。{mid_alignment.detail}")
    if alignment.state == "undetermined":
        A("- 说明：样本不足时（如上市未满 250 个交易日）不得声称“完全多头/空头排列”。")
    A("")
    A(f"- 250日最高（复权）{fmt(last['high_250'])}，最低 {fmt(last['low_250'])}，"
      f"当前距250日高点 {pct(last['drawdown_from_250h'])}")
    A("")

    # —— 动量 ——
    A("## 5. 动量与超买超卖")
    A("")
    A("| 指标 | 值 | 判读 |")
    A("|---|---|---|")
    A(f"| MACD DIF | {fmt(last['dif'], 3)} | {'零轴上方' if last['dif'] > 0 else '零轴下方'} |")
    A(f"| MACD DEA | {fmt(last['dea'], 3)} | {'金叉状态' if last['dif'] > last['dea'] else '死叉状态'} |")
    A(f"| MACD 柱 | {fmt(last['macd'], 3)} | {'红柱' if last['macd'] > 0 else '绿柱'} |")
    A(f"| RSI14（Wilder，主口径） | {fmt(last['rsi14'])} | "
      f"{'超买>70' if last['rsi14'] > 70 else ('超卖<30' if last['rsi14'] < 30 else '中性区间')} |")
    A(f"| RSI14（简单均值，仅对照） | {fmt(last['rsi14_sma'])} | 与主口径差异 "
      f"{fmt(last['rsi14_sma'] - last['rsi14'])} —— 引用前先确认口径 |")
    A(f"| KDJ (K/D/J) | {fmt(last['k'])}/{fmt(last['d'])}/{fmt(last['j'])} | "
      f"{'K>D 偏多' if last['k'] > last['d'] else 'K<D 偏空'} |")
    A(f"| BOLL %B | {fmt(last['boll_pctb'], 3)} | "
      f"{'上轨外' if last['boll_pctb'] > 1 else ('下轨外' if last['boll_pctb'] < 0 else '带内')} |")
    A(f"| BOLL 上/中/下 | {fmt(last['boll_up'])}/{fmt(last['boll_mid'])}/{fmt(last['boll_low'])} | — |")
    A("")
    A("累计收益：" + "  ".join(f"{n}日 {pct(last[f'ret_{n}d'])}" for n in (5, 20, 60, 120, 250)))
    A("")
    A("> **口径红线**：RSI 以 Wilder 平滑为准。简单均值法在剧烈单边行情下会显著高估超卖程度，"
      "据此判定「超跌反抽」属于论证错误。")
    A("")

    # —— 关键价位 ——
    A("## 6. 关键价位（复权口径自动测算）")
    A("")
    cur_p = float(last["adj_close"])
    cand = []
    for n in (20, 60, 120, 250):
        if len(px) >= n:
            cand.append((f"{n}日最低", float(px["adj_low"].tail(n).min()), "支撑"))
            cand.append((f"{n}日最高", float(px["adj_high"].tail(n).max()), "压力"))
    for lab, v in (("BOLL下轨", last["boll_low"]), ("BOLL中轨", last["boll_mid"]),
                   ("BOLL上轨", last["boll_up"])):
        if pd.notna(v):
            cand.append((lab, float(v), "支撑" if v < cur_p else "压力"))
    for n in (5, 10, 20, 30, 60, 120, 250):
        v = last.get(f"ma{n}")
        if pd.notna(v):
            cand.append((f"MA{n}", float(v), "支撑" if v < cur_p else "压力"))
    sup = sorted([c for c in cand if c[2] == "支撑"], key=lambda x: -x[1])
    res = sorted([c for c in cand if c[2] == "压力"], key=lambda x: x[1])
    A(f"现价（复权）**{fmt(cur_p)}**。以下为脚本自动测算，写报告时可直接引用，"
      "但**必须结合形态与缺口做二次判断**，机械套用均线会被假突破骗。")
    A("")
    A("| 类型 | 价位 | 依据 | 距现价 |")
    A("|---|---|---|---|")
    for c in sup[:5]:
        A(f"| 支撑 | {fmt(c[1])} | {c[0]} | {pct(c[1]/cur_p-1)} |")
    for c in res[:5]:
        A(f"| 压力 | {fmt(c[1])} | {c[0]} | {pct(c[1]/cur_p-1)} |")
    A("")
    if sup and res:
        A(f"- 最近支撑 {fmt(sup[0][1])}（{sup[0][0]}），最近压力 {fmt(res[0][1])}（{res[0][0]}）")
        A(f"- 1×ATR 区间：{fmt(cur_p - last['atr14'])} ~ {fmt(cur_p + last['atr14'])}；"
          f"1.5×ATR 止损参考：{fmt(cur_p - 1.5*last['atr14'])}；2×ATR：{fmt(cur_p - 2*last['atr14'])}")
    A("")

    # —— 近20日 ——
    A("## 7. 近 20 个交易日")
    A("")
    cols = ["date", "open", "high", "low", "close", "pctChg", "turn", "amount", "vol_ratio20", "ma20", "ma60"]
    t = px.tail(20)[cols].copy()
    t["date"] = t["date"].dt.strftime("%Y-%m-%d")
    t["amount"] = (t["amount"] / 1e5).round(2)
    t.columns = ["日期", "开", "高", "低", "收", "涨跌%", "换手%", "成交额(亿)", "量比", "MA20", "MA60"]
    A("| " + " | ".join(t.columns) + " |")
    A("|" + "---|" * len(t.columns))
    for _, r in t.iterrows():
        A("| " + " | ".join("—" if pd.isna(v) else (f"{v:g}" if isinstance(v, float) else str(v))
                            for v in r.values) + " |")
    A("")
    A(f"近5日均换手 {fmt(px['turn'].tail(5).mean())}%，近20日均换手 {fmt(px['turn'].tail(20).mean())}%，"
      f"近60日均换手 {fmt(px['turn'].tail(60).mean())}%")
    A("")

    # —— 财务 ——
    A("## 8. 财务摘要（报表 / 单季 / 官方扣非 / 调整情景）")
    A("")
    A("> 四类数字必须分开引用：**报表值**是原披露累计数；**单季值**由同年度相邻累计数相减；"
      "**官方扣非归母**来自公司公告；**分析调整情景**是本模块按明确假设算出的核对值，"
      "不等于公司真实可持续盈利。")
    A("")
    if fin_artifacts and fin_artifacts.get("status") == "ok":
        series = fin_artifacts["series"]
        records = series.records[-8:]
        lev_by_end = {}
        if fwide is not None and len(fwide):
            for _, r in fwide.iterrows():
                ta, tl = r.get("total_assets"), r.get("total_liab")
                if pd.notna(ta) and pd.notna(tl) and ta:
                    lev_by_end[str(r["end_date"])] = tl / ta
        A("| 报告期 | 单季营收(亿) | 单季归母(亿) | 单季毛利率 | 单季归母净利率 | "
          "ROE估算(简单年化) | 资产负债率(期末) | 单季经营现金流(亿) | 季度状态 |")
        A("|---|---|---|---|---|---|---|---|---|")
        for rec in records:
            def qv(field):
                cell = rec.values.get(field)
                return cell.single if cell is not None and cell.status == "ok" else None

            qrev, qni = qv("revenue"), qv("n_income_attr_p")
            qgp = (qrev - qv("oper_cost")) if (qrev is not None and qv("oper_cost") is not None) else None
            gm = qgp / qrev if qgp is not None and qrev else None
            nm = qni / qrev if qni is not None and qrev else None
            eq_cell = rec.values.get("total_hldr_eqy_exc_min_int")
            eq = eq_cell.cumulative if eq_cell is not None else None
            roe_est = qni / eq * (12 / (rec.quarter * 3)) if (qni is not None and eq) else None
            lev = lev_by_end.get(rec.end_date)
            cash = qv("n_cashflow_act")
            status = "正常" if rec.status == "ok" else f"{rec.status}：{rec.reason}"
            A(f"| {rec.end_date} | {fmt(qrev/1e8) if qrev is not None else '—'} | "
              f"{fmt(qni/1e8) if qni is not None else '—'} | "
              f"{pct(gm) if gm is not None else '—'} | {pct(nm) if nm is not None else '—'} | "
              f"{pct(roe_est) if roe_est is not None else '—'} | "
              f"{pct(lev) if lev is not None else '—'} | "
              f"{fmt(cash/1e8) if cash is not None else '—'} | {status} |")
        A("")
        A("**口径定义（可直接复算）：**")
        A("")
        A("- 单季营收 = 本期累计营业收入 − 上一季累计营业收入；Q1 单季 = Q1 累计。")
        A("- 单季毛利率 = (单季营业收入 − 单季营业成本) ÷ 单季营业收入。")
        A("- 单季归母净利率 = 单季归母净利润 ÷ 单季营业收入。")
        A("- **期末净资产口径简单年化 ROE 估算** = 单季归母净利润 ÷ 期末归母净资产 "
          "× (12 ÷ 报告期月数)。这是估算，**不是公司披露的加权平均净资产收益率**；"
          "官方加权 ROE 必须单列来源与期间，不能用 PB/PE 隐含比值代替。")
        A("- 资产负债率 = 期末负债总额 ÷ 期末资产总额（时点存量，不做单季差分）。")
        A("")

        adv = fin_artifacts.get("adjustments", {})
        rows = [row for row in adv.get("periods", []) if row.get("reported_parent_net") is not None][-8:]
        if rows:
            A("**三类盈利分列（单位：亿元）**")
            A("")
            A("| 报告期 | 报表归母 | 官方扣非归母 | 调整情景：仅剔资产减值 | 调整情景：再剔信用减值 |")
            A("|---|---|---|---|---|")
            for row in rows:
                def amt(key):
                    value = row.get(key)
                    return fmt(value / 1e8) if value is not None else "缺失"

                A(f"| {row['end_date']} | {amt('reported_parent_net')} | "
                  f"{amt('official_deducted_parent_net')} | "
                  f"{amt('adjusted_parent_net_strip_asset')} | "
                  f"{amt('adjusted_parent_net_strip_asset_and_credit')} |")
            A("")
            scenario = adv.get("scenario", {})
            A(f"- 调整情景公式：{scenario.get('formula', '—')}")
            A(f"- 情景假设：{'；'.join(scenario.get('assumptions', []))}")
            A("- **调整后单季利润不得乘 4 当作可持续年利润**；它只是这次核对口径下的一个情景。")
            A("")

        cash_note = fin_artifacts["series"].records[-1] if fin_artifacts["series"].records else None
        if cash_note is not None:
            cell_cash = cash_note.values.get("n_cashflow_act")
            cell_net = cash_note.values.get("n_income")
            cell_parent = cash_note.values.get("n_income_attr_p")
            if (cell_cash is not None and cell_cash.single is not None
                    and cell_net is not None and cell_net.single not in (None, 0)):
                A(f"- 经营现金流/合并净利润（最新单季，同口径）= "
                  f"{fmt(cell_cash.single / cell_net.single * 100)}%；"
                  "分子分母同为合并口径，可作一致性指标，但单一季度比值不构成“健康”结论。")
            if (cell_cash is not None and cell_cash.single is not None
                    and cell_parent is not None and cell_parent.single not in (None, 0)):
                A(f"- 经营现金流/归母净利润（最新单季）= "
                  f"{fmt(cell_cash.single / cell_parent.single * 100)}%；"
                  "分母不含少数股东损益，与分子口径不匹配，仅作参考。")
        A("")
        lr = fwide.iloc[-1] if fwide is not None and len(fwide) else None
        if lr is not None:
            A("**最新期关键项（期末时点值）**：")
            for k, lab, unit in (("money_cap", "货币资金", "亿"), ("accounts_receiv", "应收账款", "亿"),
                                 ("notes_receiv", "应收票据", "亿"), ("inventories", "存货", "亿"),
                                 ("goodwill", "商誉", "亿"), ("fix_assets", "固定资产", "亿"),
                                 ("cip", "在建工程", "亿"), ("st_borr", "短期借款", "亿"),
                                 ("lt_borr", "长期借款", "亿"), ("bond_payable", "应付债券", "亿")):
                if k in lr and pd.notna(lr[k]):
                    A(f"- {lab}：{fmt(lr[k]/1e8)} {unit}")
        A("")
    elif fwide is not None and len(fwide):
        A("_财务原表可用，但季度拆分不可得；原因见 financial_quarters.csv 的 quarter_status 列。_")
        A("")
    else:
        A("_财务三表均无该标的记录，基本面分析必须走 Level 2/3 补齐。_")
        A("")

    # —— 预告 ——
    A("## 9. 业绩预告 / 快报（最高优先级催化）")
    A("")
    st = REG.status_of("forecast")
    last_ann_disp = str(fc["ann_date"].max()) if (fc is not None and len(fc)) else "—"
    if st == "OK" and fc is not None and len(fc):
        A(f"本地命中 {len(fc)} 条（{fc['ann_date'].min()} ~ {fc['ann_date'].max()}），最近 5 条：")
        A("")
        A("| 公告日 | 报告期 | 类型 | 净利下限(亿) | 净利上限(亿) | 同比下限 | 同比上限 | 摘要 |")
        A("|---|---|---|---|---|---|---|---|")
        for _, r in fc.tail(5).iterrows():
            A(f"| {r.get('ann_date')} | {r.get('end_date')} | {r.get('type')} | "
              f"{fmt(r.get('net_profit_min', np.nan)/1e4)} | {fmt(r.get('net_profit_max', np.nan)/1e4)} | "
              f"{fmt(r.get('p_change_min'))}% | {fmt(r.get('p_change_max'))}% | "
              f"{str(r.get('summary') or '')[:40].replace('|','/')} |")
    elif st == "NO_RECORD":
        A("**本地该标的无任何业绩预告记录。** 注意：这是「无记录」，不是「数据不存在」。"
          "若基准日临近财报季，必须用 Level 3 检索交易所/互动易核实。")
    else:
        A(f"**业绩预告数据不可用（{st}）**，必须走 Level 3 检索核实。")
    if fresh_gap is not None and fresh_gap > 5:
        A("")
        A(f"> **时效警告**：本地业绩预告最新文件距基准日 {fresh_gap} 天。"
          "预告是最高优先级催化，必须用 Level 3 单独检索一次，不能以「本地没有」作结论。")
    if fc_gap is not None and fc_gap > 120:
        A("")
        A(f"> **个股级时效警告**：该股最新一条预告停留在 {fc_gap} 天前（{last_ann_disp}），"
          "而本地数据集本身仍在更新。"
          "这正是「数据集有数据 ≠ 这只股票的催化已被捕捉」的典型漏点 —— "
          "必须用 Level 3 检索该股是否已披露新一期业绩预告（交易所公告 / 互动易 / 巨潮）。"
          "**未核实之前，不得在报告中写「尚未披露业绩预告」。**")
    elif fc_gap is not None and fc_gap > 30 and ("0101" <= asof[5:7] + asof[8:10] <= "0215"
                                                 or "0401" <= asof[5:7] + asof[8:10] <= "0515"
                                                 or "0701" <= asof[5:7] + asof[8:10] <= "0831"
                                                 or "1001" <= asof[5:7] + asof[8:10] <= "1115"):
        A("")
        A(f"> **个股级时效警告**：基准日处于**业绩预告披露窗口**，但该股最新预告仍是 "
          f"{fc_gap} 天前的 {last_ann_disp}。本地数据集文件已更新到最新，说明限制不在管线而在覆盖。"
          "**动笔前必须 Level 3 核实该股是否已披露新预告；未核实不得写「尚未披露」。**")
    elif st == "NO_RECORD":
        A("")
        A("> **零记录警告**：该股在本地预告库中完全没有记录。若基准日处于财报季，"
          "必须 Level 3 核实；不得直接下结论说公司没有披露预告。")
    A("")

    # —— 行业 ——
    A("## 10. 行业定位")
    A("")
    if ind_info is not None and len(ind_info):
        for _, r in ind_info.iterrows():
            A(f"- 行业：**{r.get('name', '—')}**（{r.get('ind_code')}）　成分股 {fmt(r.get('count', np.nan), 0)} 只")
            if pd.notna(r.get("avg_pct", np.nan)):
                A(f"- 行业当日均涨 {fmt(r.get('avg_pct'))}%，全行业排名 "
                  f"{fmt(r.get('rank_1d', np.nan), 0)}/{fmt(r.get('total_industries', np.nan), 0)}")
            if pd.notna(r.get("cum20_pct", np.nan)):
                A(f"- 行业近20日累计 {fmt(r.get('cum20_pct'))}%，排名 "
                  f"{fmt(r.get('rank_20d', np.nan), 0)}/{fmt(r.get('total_industries', np.nan), 0)}")
            if pd.notna(r.get("breadth", np.nan)):
                A(f"- 行业上涨家数占比（breadth）{fmt(float(r.get('breadth'))*100)}%")
    else:
        A("_无行业归属记录_")
    A("")

    # —— 资金情绪 ——
    A("## 11. 资金与情绪摘要")
    A("")
    mfp = out_dir / "moneyflow.csv"
    if mfp.exists():
        m = pd.read_csv(mfp)
        m["trade_date"] = m["trade_date"].astype(str)
        num = ["buy_elg_amount", "sell_elg_amount", "buy_lg_amount", "sell_lg_amount"]
        if all(col in m.columns for col in num):
            for col in num:
                m[col] = pd.to_numeric(m[col], errors="coerce").fillna(0)
            # Tushare moneyflow 的 amount 字段单位为万元
            m["elg_net"] = m["buy_elg_amount"] - m["sell_elg_amount"]
            m["lg_net"] = m["buy_lg_amount"] - m["sell_lg_amount"]
            m["main_net"] = m["elg_net"] + m["lg_net"]
            A("主力净额 = 超大单净额 + 大单净额（单位：亿元）。")
            A("")
            A("| 区间 | 主力净额 | 超大单净额 | 大单净额 |")
            A("|---|---|---|---|")
            for n, lab in ((1, "当日"), (5, "近5日"), (20, "近20日"), (60, "近60日")):
                A(f"| {lab} | {fmt(m['main_net'].tail(n).sum()/1e4)} | "
                  f"{fmt(m['elg_net'].tail(n).sum()/1e4)} | {fmt(m['lg_net'].tail(n).sum()/1e4)} |")
            A("")
            if "net_mf_amount" in m.columns:
                m["net_mf_amount"] = pd.to_numeric(m["net_mf_amount"], errors="coerce").fillna(0)
                A(f"- 全单型净额（net_mf_amount，当日）：{fmt(m['net_mf_amount'].iloc[-1]/1e4)} 亿元"
                  "　（含中单/小单，通常显著小于主力净额，勿混用）")
            row = m.iloc[-1]
            A(f"- 当日超大单/大单拆解：超大单 {fmt(row['elg_net']/1e4)} 亿，大单 {fmt(row['lg_net']/1e4)} 亿")
            if abs(row["elg_net"]) > 0:
                A(f"- 超大单占主力净额比重 {fmt(row['elg_net']/row['main_net']*100 if row['main_net'] else np.nan, 1)}%"
                  "　（>80% 说明是机构/大资金单边行为，而非散户跟风）")
            A("")
    mdp = out_dir / "sent_margin_detail.csv"
    if mdp.exists():
        md = pd.read_csv(mdp)
        md["date"] = pd.to_datetime(md["date"])
        md = md.sort_values("date")
        A(f"- 两融余额：{fmt(md['rzrqye'].iloc[-1]/1e8)} 亿元（{md['date'].iloc[-1].date()}）")
        if len(md) > 20:
            A(f"- 20 日前：{fmt(md['rzrqye'].iloc[-21]/1e8)} 亿元，"
              f"变化 {pct(md['rzrqye'].iloc[-1]/md['rzrqye'].iloc[-21]-1)}")
        A(f"- 融资余额 {fmt(md['rzye'].iloc[-1]/1e8)} 亿，融券余额 {fmt(md['rqye'].iloc[-1]/1e8)} 亿")
        A("")
    tlp = out_dir / "sent_top_list.csv"
    if tlp.exists():
        tl = pd.read_csv(tlp)
        A(f"- 龙虎榜上榜 {len(tl)} 次（{tl['date'].min()} ~ {tl['date'].max()}），"
          f"合计净买入 {fmt(tl['net_amount'].sum()/1e8)} 亿元（net_amount 单位=元）")
        A("")
    if not (mfp.exists() or mdp.exists()):
        A("_无资金/情绪数据_")
        A("")

    # —— 强制检查清单 ——
    A("## 12. 写作前强制检查清单")
    A("")
    fc_status = REG.status_of("forecast")
    fc_txt = (f"本地 {len(fc)} 条，最新 {last_ann_disp}（距基准日 {fc_gap} 天）"
              if (fc is not None and len(fc) and fc_gap is not None)
              else "本地无命中，必须 Level 3 核实")
    checklist = [
        f"行情 {REG.status_of('price')}：{len(px)} 根K线。支撑/压力/止损/ATR/前高前低是否**全部**用复权价？",
        f"复权 {'异常' if af_flag else 'OK'}：报告中价格水平类指标与复权口径是否一致？混用未复权价会系统性偏移。"
        "复权价是前复权（基准日=" + asof + "），真实委托价要单独换算。",
        "均线排列：是否按“相邻严格不等”判断？出现相等值或缺值时应写混合/不可判，"
        "不得用排序结果代替严格不等式。",
        f"业绩预告 {fc_status}：{fc_txt}。"
        + ("**距基准日超过 120 天，必须先 Level 3 核实是否已有新预告，再动笔。**"
           if (fc_gap or 0) > 120 else ""),
        f"财务三表 {REG.status_of('financials')}：单季值是否取自 financial_quarters.csv，"
        "且 quarter_status 为 ok？缺上一季时是否明确写“单季不可得”，而不是倒推？",
        "盈利口径：报表归母、官方扣非、调整情景是否分列？调整情景是否写明税率、"
        "少数股东处理与“不是可持续年利润”？",
        "ROE：是否写的是“期末净资产口径简单年化估算”，而不是公司披露的加权平均 ROE？",
        f"估值分位：PE/PB/PS 是否给出**历史分位**而非仅绝对值？分位区间是否与结论匹配？",
        f"资金 {REG.status_of('moneyflow')}：当日主力净额是否拆到超大单/大单？net_mf_amount 与主力净额是否混用？",
        f"两融 {REG.status_of('margin_detail')}：T+1 发布，最新日期 {REG.get('margin_detail')['span'].split('~')[-1].strip() if REG.get('margin_detail') else '—'}，"
        "报告中不得当作基准日当天数据。",
        f"龙虎榜 {REG.status_of('top_list')}：上榜/未上榜都要解释；未上榜不等于没有大资金。",
        f"行业 {REG.status_of('industry')}：是否给出**排名数字**（如 20 日 122/131），而非只说「板块偏弱」？",
        f"新闻 {REG.status_of('news')}：本地 news 停更期之后的事件，每条都要标 Level 3 来源与日期。",
        "所有「数据缺口」表述：是否与第 1 节矩阵的 NO_RECORD / DATASET_ABSENT 一字不差地对应？"
        "**禁止把 NO_RECORD 写成「数据不存在」。**",
        "单位自检：amount 千元 / volume 股 / moneyflow 万元 / 两融元 / 龙虎榜 net_amount 元 —— 换算是否都写对？",
        "决策一致性：方向（BUY/SELL/HOLD）、仓位、入场区间、止损、目标价五者互不矛盾？",
        "图表信息量：每条参考线、每根柱子是否承载数据？出现空数组或 2-3 个点的「凑数图」即视为不合格。",
    ]
    for i, item in enumerate(checklist, 1):
        A(f"{i}. [ ] {item}")
    A("")

    (out_dir / "DATA_DIGEST.md").write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="个股分析数据包提取器（Stage 0）")
    ap.add_argument("--code", required=True, help="600487.SH / sh.600487 / 600487")
    ap.add_argument("--asof", default=None, help="基准日 YYYY-MM-DD 或 YYYYMMDD，默认取最新交易日")
    ap.add_argument("--out", default=None, help="输出目录，默认 ./.workbuddy/trading/<code6>/data")
    ap.add_argument("--name", default=None, help="股票中文名（覆盖自动识别）")
    ap.add_argument("--years", type=int, default=8, help="行情历史深度（年），默认 8")
    args = ap.parse_args()

    code6 = norm_code(args.code)[2]
    out = Path(args.out).expanduser() if args.out else \
        Path.cwd() / ".workbuddy" / "trading" / code6 / "data"

    try:
        build(args.code, args.asof, out, args.years, args.name)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
