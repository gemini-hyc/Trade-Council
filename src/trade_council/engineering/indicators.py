"""Deterministic technical indicators used by analysis methods."""

from __future__ import annotations

import numpy as np
import pandas as pd


def wilder_rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder-smoothed RSI, the primary RSI definition."""

    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    avg_up = up.ewm(alpha=1 / n, adjust=False).mean()
    avg_down = down.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + avg_up / avg_down.replace(0, np.nan))


def sma_rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Simple-average RSI kept only as a comparison definition."""

    delta = close.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta).clip(lower=0).rolling(n).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Add deterministic price, momentum, volatility, and volume indicators."""

    df = frame.sort_values("date").reset_index(drop=True)
    close = df["adj_close"]
    high = df["adj_high"]
    low = df["adj_low"]

    for n in (5, 10, 20, 30, 60, 120, 250):
        df[f"ma{n}"] = close.rolling(n).mean()
        df[f"dist_ma{n}"] = close / df[f"ma{n}"] - 1

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2

    df["rsi14"] = wilder_rsi(close)
    df["rsi14_sma"] = sma_rsi(close)

    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    df["k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    df["d"] = df["k"].ewm(alpha=1 / 3, adjust=False).mean()
    df["j"] = 3 * df["k"] - 2 * df["d"]

    mid = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    df["boll_mid"] = mid
    df["boll_up"] = mid + 2 * sd
    df["boll_low"] = mid - 2 * sd
    band = (df["boll_up"] - df["boll_low"]).replace(0, np.nan)
    df["boll_pctb"] = (close - df["boll_low"]) / band

    previous = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    df["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    df["atr_pct"] = df["atr14"] / close * 100

    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio20"] = df["volume"] / df["vol_ma20"]
    df["amount_ma5"] = df["amount"].rolling(5).mean()

    for n in (1, 5, 20, 60, 120, 250):
        df[f"ret_{n}d"] = close / close.shift(n) - 1

    df["high_250"] = high.rolling(250, min_periods=20).max()
    df["low_250"] = low.rolling(250, min_periods=20).min()
    df["drawdown_from_250h"] = close / df["high_250"] - 1
    return df


def main_net_amount(moneyflow: pd.DataFrame) -> pd.Series:
    """Return large/extra-large order net amount in the source moneyflow unit."""

    required = {
        "buy_elg_amount",
        "sell_elg_amount",
        "buy_lg_amount",
        "sell_lg_amount",
    }
    missing = required - set(moneyflow.columns)
    if missing:
        raise ValueError(f"moneyflow missing columns: {sorted(missing)}")
    return (
        moneyflow["buy_elg_amount"].fillna(0)
        - moneyflow["sell_elg_amount"].fillna(0)
        + moneyflow["buy_lg_amount"].fillna(0)
        - moneyflow["sell_lg_amount"].fillna(0)
    )

