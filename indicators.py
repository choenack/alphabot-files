# -*- coding: utf-8 -*-
"""
indicators.py — 지표 계산 + LLM이 읽기 쉬운 라벨로 변환
=====================================================
원칙: 원본 가격 대신 '상대값(%)'과 '라벨'로 넘긴다. 계산은 코드가, 종합 판단만 LLM이.
"""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    ag = gain.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    rs = ag / al.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.where(al != 0, 100.0)


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    m = ema(close, fast) - ema(close, slow)
    return m - ema(m, sig)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()


def bb_pos(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """볼린저 밴드 안 위치: 하단 0, 상단 100."""
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    lo, hi = ma - k * sd, ma + k * sd
    return ((close - lo) / (hi - lo).replace(0.0, np.nan) * 100.0)


# ── 라벨 ────────────────────────────────────────────────────────────
def trend_label(df: pd.DataFrame) -> str:
    """종가 > EMA20 > EMA50 이면 UP, 반대면 DOWN, 그 외 SIDE."""
    if len(df) < 50:
        return "SIDE"
    c = df["close"]
    e20, e50 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1]
    last = c.iloc[-1]
    if last > e20 > e50:
        return "UP"
    if last < e20 < e50:
        return "DOWN"
    return "SIDE"


def macd_state(close: pd.Series) -> str:
    h = macd_hist(close)
    if len(h.dropna()) < 3:
        return "FLAT"
    a, b = float(h.iloc[-2]), float(h.iloc[-1])
    if a <= 0 < b:
        return "CROSS_UP"
    if a >= 0 > b:
        return "CROSS_DOWN"
    if b > 0:
        return "POS_RISING" if b > a else "POS_FADING"
    return "NEG_RECOVERING" if b > a else "NEG_FALLING"


def rsi_dir(r: pd.Series) -> str:
    if len(r.dropna()) < 2:
        return "FLAT"
    d = float(r.iloc[-1] - r.iloc[-2])
    return "UP" if d > 1 else "DOWN" if d < -1 else "FLAT"


def momentum_label(df15: pd.DataFrame, bars: int = 4) -> str:
    """최근 15분봉 4개(1시간) 흐름."""
    if len(df15) < bars + 1:
        return "FLAT"
    chg = df15["close"].iloc[-1] / df15["close"].iloc[-1 - bars] - 1
    if chg > 0.01:
        return "STRONG_UP"
    if chg > 0.002:
        return "UP"
    if chg < -0.01:
        return "STRONG_DOWN"
    if chg < -0.002:
        return "DOWN"
    return "FLAT"


def pct(a: float, b: float) -> float:
    """a가 b보다 몇 % 위인지."""
    return round((a / b - 1) * 100, 2) if b else 0.0
