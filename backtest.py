# -*- coding: utf-8 -*-
"""
backtest.py — 최근 업비트 데이터로 '셋업 + 손절·트레일링' 규칙을 다시 돌려 보는 도구
=====================================================
· LLM 없이 '잡힌 셋업은 전부 진입'한다고 가정 → 코드 규칙 자체가 돈을 버는지 본다(LLM은 여기서 더 거르는 역할).
· 봇과 같은 함수(strategy.detect_setups, risk.regime_mult)와 같은 손절 규칙을 15분봉으로 재현한다.
  - 돌파: 다음 15분봉 시가에 시장가 진입 / 눌림목: 지정가가 60분 안에 닿으면 체결
  - 손절선을 건드리면 그 값(갭이면 시가)에 매도. 한 봉 안에서 고가·저가 순서를 모르면 '손절 먼저'(보수적)
  - 수수료 0.05%×2 + 슬리피지 0.05%×2 반영
· 한계: 오늘 기준 거래대금 상위 코인으로 과거를 본다(생존 편향), 기간이 짧으면 운에 크게 좌우된다.
실행: python backtest.py --days 14
"""
import argparse
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import db
import indicators as ind
import risk
from config import cfg
from strategy import BtcContext, Scanner, detect_setups
from upbit import Upbit

SLIP = 0.0005
_UNIT = {"minutes/15": timedelta(minutes=15), "minutes/60": timedelta(hours=1),
         "minutes/240": timedelta(hours=4), "days": timedelta(days=1)}


# ── 데이터 ──────────────────────────────────────────────────────────
def history(ub: Upbit, market: str, unit: str, n: int) -> pd.DataFrame:
    """마감된 봉 n개(이상)를 과거로 넘겨 가며 모은다."""
    parts, to, got = [], None, 0
    while got < n:
        df = ub.candles(market, unit, 200, to=to, closed_only=False)
        if df.empty:
            break
        parts.insert(0, df)
        got += len(df)
        to = df["ts"].iloc[0].strftime("%Y-%m-%dT%H:%M:%SZ")
        if len(df) < 200:
            break
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df[df["ts"] + _UNIT[unit] <= datetime.now(timezone.utc)].reset_index(drop=True)


def load(days: int, cache: Path | None = None) -> dict:
    if cache and cache.exists():
        return pickle.loads(cache.read_bytes())
    ub = Upbit()
    uni = Scanner(ub).refresh_universe(force=True)
    data = {"universe": uni, "coins": {}, "btc": {
        "days": history(ub, "KRW-BTC", "days", 150),
        "h4": history(ub, "KRW-BTC", "minutes/240", 6 * days + 130),
        "h1": history(ub, "KRW-BTC", "minutes/60", 24 * days + 60)}}
    for m in uni:
        data["coins"][m] = {"m15": history(ub, m, "minutes/15", 96 * days + 60),
                            "h1": history(ub, m, "minutes/60", 24 * days + 230),
                            "h4": history(ub, m, "minutes/240", 6 * days + 130)}
    if cache:
        cache.write_bytes(pickle.dumps(data))
    return data


def _upto(df: pd.DataFrame, ends: np.ndarray, t, n: int) -> pd.DataFrame:
    k = int(np.searchsorted(ends, np.datetime64(t), side="right"))
    return df.iloc[max(0, k - n):k].reset_index(drop=True)


def _ends(df: pd.DataFrame, unit: str) -> np.ndarray:
    return (df["ts"] + _UNIT[unit]).values.astype("datetime64[ns]")


def btc_at(btc: dict, ends: dict, t) -> BtcContext:
    d1 = _upto(btc["days"], ends["days"], t, 80)
    h4 = _upto(btc["h4"], ends["h4"], t, 100)
    h1 = _upto(btc["h1"], ends["h1"], t, 30)
    ctx = BtcContext()
    if len(d1) >= 50 and len(h4) >= 50:
        d_up = d1["close"].iloc[-1] > ind.ema(d1["close"], 50).iloc[-1]
        e20, e50 = ind.ema(h4["close"], 20).iloc[-1], ind.ema(h4["close"], 50).iloc[-1]
        ctx.regime = "UP" if d_up and e20 > e50 else "DOWN" if (not d_up and e20 < e50) else "SIDE"
    if len(h1) >= 25:
        ctx.price = float(h1["close"].iloc[-1])
        ctx.ret24 = ind.pct(ctx.price, float(h1["close"].iloc[-25]))
    if len(h4) >= 43:
        ctx.ret7d = ind.pct(float(h4["close"].iloc[-1]), float(h4["close"].iloc[-43]))
    return ctx


# ── 매매 1건 재현 ───────────────────────────────────────────────────
def run_trade(s, m15: pd.DataFrame, i: int) -> dict | None:
    fee = cfg.FEE_RATE
    n = len(m15)
    if s.entry_type == "market":
        if i + 1 >= n:
            return None
        j0, entry = i + 1, float(m15["open"].iloc[i + 1]) * (1 + SLIP)
    else:
        j0 = None
        for j in range(i + 1, min(n, i + 1 + cfg.PULLBACK_EXPIRY_MIN // 15)):
            if float(m15["low"].iloc[j]) <= s.limit_price:
                j0, entry = j, float(s.limit_price)
                break
        if j0 is None:
            return {"unfilled": True}
    stop = float(s.stop_price)
    if stop >= entry * 0.995:   # trader._activate와 같은 방어
        stop = entry * (1 - max(0.01, (s.price - stop) / s.price))
    r_val, atr, peak, be = entry - stop, float(s.atr_abs), entry, False
    t_open = m15["ts"].iloc[j0]
    reason, exit_px, k = "open", float(m15["close"].iloc[-1]), n - 1
    for k in range(j0, n):
        o, h, lo, c = (float(m15[x].iloc[k]) for x in ("open", "high", "low", "close"))
        if lo <= stop:
            exit_px, reason = min(o, stop) * (1 - SLIP), ("trail" if be else "stop")
            break
        peak = max(peak, h)
        if not be and peak >= entry + cfg.BE_TRIGGER_R * r_val:
            be = True
            stop = max(stop, entry * (1 + 2 * fee + 0.001))
        if be:
            stop = max(stop, peak - cfg.TRAIL_ATR * atr)
        held_h = (m15["ts"].iloc[k] + timedelta(minutes=15) - t_open).total_seconds() / 3600
        if not be and held_h >= s.time_stop_h:
            exit_px, reason = c * (1 - SLIP), "time"
            break
    net = (exit_px * (1 - fee) - entry * (1 + fee)) / entry
    return {"market": s.market, "setup": s.setup, "entry_t": t_open, "exit_i": k, "reason": reason,
            "r": net * entry / r_val if r_val > 0 else 0.0, "net_pct": net * 100,
            "hold_h": (m15["ts"].iloc[k] - t_open).total_seconds() / 3600, "stop_pct": r_val / entry * 100,
            "mfe_r": (peak - entry) / r_val if r_val > 0 else 0.0, **{f"f_{k2}": v for k2, v in s.features.items()
                                                                    if k2 in ("trend_4h", "btc_regime", "rs_24h",
                                                                              "vol_ratio_1h", "rsi_1h", "dist_high20_pct",
                                                                              "mom_15m", "atr_pct_1h")}}


def simulate(data: dict, days: int, use_regime_filter: bool = True) -> tuple[list, int]:
    btc = data["btc"]
    bends = {"days": _ends(btc["days"], "days"), "h4": _ends(btc["h4"], "minutes/240"), "h1": _ends(btc["h1"], "minutes/60")}
    start = datetime.now(timezone.utc) - timedelta(days=days)
    trades, unfilled = [], 0
    for m, d in data["coins"].items():
        m15, h1, h4 = d["m15"], d["h1"], d["h4"]
        if len(m15) < 50 or len(h1) < 80 or len(h4) < 60:
            continue
        e1, e4 = _ends(h1, "minutes/60"), _ends(h4, "minutes/240")
        asked: dict = {}
        i = int(np.searchsorted(m15["ts"].values.astype("datetime64[ns]"), np.datetime64(start.replace(tzinfo=None))))
        busy_until = -1
        while i < len(m15) - 1:
            if i <= busy_until:
                i += 1
                continue
            t_end = m15["ts"].iloc[i] + timedelta(minutes=15)
            ctx = btc_at(btc, bends, t_end)
            ss = detect_setups(m, _upto(h1, e1, t_end, 200), _upto(h4, e4, t_end, 120),
                               m15.iloc[max(0, i - 39):i + 1].reset_index(drop=True), ctx, [])
            took = False
            for s in ss:
                key = (s.setup,)
                if (t_end - asked.get(key, t_end - timedelta(days=1))).total_seconds() < cfg.SETUP_RECHECK_SEC:
                    continue
                asked[key] = t_end
                if use_regime_filter and risk.regime_mult(ctx.regime, s)[0] <= 0:
                    continue
                tr = run_trade(s, m15, i)
                if tr is None:
                    continue
                if tr.get("unfilled"):
                    unfilled += 1
                    continue
                trades.append(tr)
                busy_until = tr["exit_i"] + cfg.REENTRY_COOLDOWN_SEC // 900
                took = True
                break
            i += 1 if not took else 0
            if took:
                i = busy_until + 1
    return trades, unfilled


def summary(trades: list, label: str = "") -> str:
    if not trades:
        return f"{label}: 매매 없음"
    df = pd.DataFrame(trades)
    pos, neg = df.loc[df.r > 0, "r"].sum(), -df.loc[df.r <= 0, "r"].sum()
    pf = pos / neg if neg > 0 else float("inf")
    reasons = df["reason"].value_counts(normalize=True).mul(100).round(0).to_dict()
    out = [f"{label}: {len(df)}회 · 승률 {(df.r > 0).mean() * 100:.0f}% · 평균 {df.r.mean():+.2f}R · 합계 {df.r.sum():+.1f}R · "
           f"PF {pf:.2f} · 평균 보유 {df.hold_h.mean():.1f}h · 청산 {reasons}"]
    for k, g in df.groupby("setup"):
        out.append(f"   {k}: {len(g)}회 승률 {(g.r > 0).mean() * 100:.0f}% 평균 {g.r.mean():+.2f}R 합계 {g.r.sum():+.1f}R "
                   f"· 손절 거리 평균 {g.stop_pct.mean():.1f}% · 최대 도달 평균 {g.mfe_r.mean():.2f}R")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cache", default="")
    a = ap.parse_args()
    db.init(":memory:")
    data = load(a.days, Path(a.cache) if a.cache else None)
    trades, unfilled = simulate(data, a.days)
    print(summary(trades, f"최근 {a.days}일 (지정가 미체결 {unfilled}건)"))


if __name__ == "__main__":
    main()
