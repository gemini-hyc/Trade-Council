from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trade_council.engineering.evidence import EvidenceIndex
from trade_council.engineering.indicators import main_net_amount, sma_rsi, wilder_rsi
from trade_council.engineering.prices import adjusted_to_raw, raw_to_adjusted


def test_rsi_definitions_are_distinct() -> None:
    close = pd.Series([10, 11, 10, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 19, 18])
    wilder = wilder_rsi(close).iloc[-1]
    sma = sma_rsi(close).iloc[-1]
    assert np.isfinite(wilder)
    assert np.isfinite(sma)
    assert wilder != sma


def test_main_net_formula() -> None:
    frame = pd.DataFrame(
        {
            "buy_elg_amount": [100.0],
            "sell_elg_amount": [40.0],
            "buy_lg_amount": [30.0],
            "sell_lg_amount": [10.0],
        }
    )
    assert main_net_amount(frame).iloc[0] == 80.0


def test_adjusted_raw_round_trip() -> None:
    raw = 100.0
    adjusted = raw_to_adjusted(raw, adj_factor_at_price=2.0, adj_factor_asof=4.0)
    assert adjusted == 50.0
    assert adjusted_to_raw(adjusted, adj_factor_at_price=2.0, adj_factor_asof=4.0) == raw


def test_evidence_ids_are_content_addressed_and_validated(tmp_path: Path) -> None:
    index = EvidenceIndex(snapshot_id="snap-1")
    first = index.add(
        dataset_id="market.daily",
        path="market/daily",
        primary_key="600519.SH",
        event_date="2026-10-16",
        available_at="2026-10-16 15:00:00",
        field="close",
        value=1329.0,
        unit="CNY",
    )
    second = index.add(
        dataset_id="market.daily",
        path="market/daily",
        primary_key="600519.SH",
        event_date="2026-10-16",
        available_at="2026-10-16 15:00:00",
        field="close",
        value=1329.0,
        unit="CNY",
    )
    assert first.evidence_id == second.evidence_id
    index.validate_references([first.evidence_id])
    with pytest.raises(ValueError, match="Unknown evidence ids"):
        index.validate_references(["F-missing"])
    index.write(tmp_path)
    assert (tmp_path / "evidence.json").exists()
    assert (tmp_path / "evidence_trace.jsonl").exists()

