# -*- coding: utf-8 -*-
"""
strategy.py — 코드 1차 필터: 감시 대상 선정 + 셋업(돌파/눌림목) 감지 + LLM 입력 만들기
=====================================================
흐름
  1) 감시 대상: 원화마켓 24h 거래대금 상위 30개(스테이블·유의종목·상장 7일 미만 제외), 1시간마다 갱신
  2) BTC 상태: 일봉 EMA50 + 4시간봉 EMA20/50 → UP / SIDE / DOWN
  3) 코인별 셋업
     · 돌파:   4h 추세가 DOWN이 아님 + 15분 종가가 1h 20봉 고점 돌파 + 최근 1시간 거래량 ≥ 평소 2배
               (돌파선보다 1.5ATR 넘게 올라갔으면 추격 금지) → 바로 시장가 진입, 손절 = 돌파선 - 1ATR
     · 눌림목: 4h 추세 UP + 1h가 EMA20(+0.25ATR) 아래로 내려옴(EMA50 위) + RSI 38~55
               → 현재가 - 0.5ATR 지정가 대기, 손절 = 진입가 - 2ATR
  4) 셋업이 잡힌 코인만 LLM에 넘긴다(대부분은 여기서 걸러져 LLM 비용 0).
"""
import time
from dataclasses import dataclass, field

import db
import indicators as ind
from config import cfg
from upbit import MARKET_NAMES, Upbit

_CAUTION_KO = {
    "PRICE_FLUCTUATIONS": "PRICE_SWING", "TRADING_VOLUME_SOARING": "VOLUME_SOARING",
    "DEPOSIT_AMOUNT_SOARING": "DEPOSIT_SOARING", "GLOBAL_PRICE_DIFFERENCES": "KIMCHI_GAP",
    "CONCENTRATION_OF_SMALL_ACCOUNTS": "SMALL_ACCOUNTS",
}


@dataclass
class BtcContext:
    regime: str = "SIDE"
    ret24: float = 0.0
    ret7d: float = 0.0
    price: float = 0.0


@dataclass
class Setup:
    market: str
    setup: str                 # BREAKOUT | PULLBACK
    entry_type: str            # market | limit
    price: float               # 감지 시점 가격(마지막 15분 종가)
    limit_price: float | None
    stop_price: float
    stop_pct: float            # 진입가 대비 손절 거리(0.04 = 4%)
    atr_abs: float
    time_stop_h: float
    rs24: float
    features: dict = field(default_factory=dict)


class Scanner:
    def __init__(self, ub: Upbit):
        self.ub = ub
        self.universe: list[str] = []
        self._universe_ts = 0.0
        self._old_enough: set[str] = set()
        self._caution: dict[str, list] = {}
        self._asked: dict[tuple, float] = {}

    # ── 감시 대상 ───────────────────────────────────────────────────
    def refresh_universe(self, force: bool = False) -> list:
        if not force and self.universe and time.time() - self._universe_ts < cfg.UNIVERSE_REFRESH_SEC:
            return self.universe
        info = {m["market"]: m for m in self.ub.markets() if m["market"].startswith("KRW-")}
        MARKET_NAMES.update({m: x.get("korean_name") or m for m, x in info.items()})
        caution, blocked = {}, set()
        for m, x in info.items():
            ev = x.get("market_event") or {}
            if ev.get("warning"):
                blocked.add(m)
            flags = [_CAUTION_KO.get(k, k) for k, v in (ev.get("caution") or {}).items() if v]
            if flags:
                caution[m] = flags
        self._caution = caution

        tickers = sorted(self.ub.tickers_all("KRW"), key=lambda t: -float(t.get("acc_trade_price_24h") or 0))
        out = []
        for t in tickers:
            m = t["market"]
            if m not in info or m in blocked or m.split("-", 1)[1] in cfg.STABLE_COINS:
                continue
            if m not in self._old_enough:
                d = self.ub.candles(m, "days", count=cfg.MIN_LISTING_DAYS + 1, closed_only=False)
                if len(d) < cfg.MIN_LISTING_DAYS + 1:
                    continue
                self._old_enough.add(m)
            out.append(m)
            if len(out) >= cfg.UNIVERSE_SIZE:
                break
        self.universe, self._universe_ts = out, time.time()
        return out

    # ── BTC 상태 ────────────────────────────────────────────────────
    def btc_context(self) -> BtcContext:
        d1 = self.ub.candles("KRW-BTC", "days", 80)
        h4 = self.ub.candles("KRW-BTC", "minutes/240", 100)
        h1 = self.ub.candles("KRW-BTC", "minutes/60", 30, closed_only=False)
        ctx = BtcContext()
        if len(d1) >= 50 and len(h4) >= 50:
            dc = d1["close"]
            d_up = dc.iloc[-1] > ind.ema(dc, 50).iloc[-1]
            e20, e50 = ind.ema(h4["close"], 20).iloc[-1], ind.ema(h4["close"], 50).iloc[-1]
            if d_up and e20 > e50:
                ctx.regime = "UP"
            elif not d_up and e20 < e50:
                ctx.regime = "DOWN"
        if len(h1) >= 25:
            ctx.price = float(h1["close"].iloc[-1])
            ctx.ret24 = ind.pct(ctx.price, float(h1["close"].iloc[-25]))
        if len(h4) >= 43:
            ctx.ret7d = ind.pct(float(h4["close"].iloc[-1]), float(h4["close"].iloc[-43]))
        return ctx

    # ── 코인별 셋업 ─────────────────────────────────────────────────
    def analyze(self, market: str, btc: BtcContext) -> list:
        h1 = self.ub.candles(market, "minutes/60", 200)
        h4 = self.ub.candles(market, "minutes/240", 120)
        m15 = self.ub.candles(market, "minutes/15", 40)
        return detect_setups(market, h1, h4, m15, btc, self._caution.get(market, []))

    def recently_asked(self, s: Setup) -> bool:
        return time.time() - self._asked.get((s.market, s.setup), 0) < cfg.SETUP_RECHECK_SEC

    def mark_asked(self, s: Setup) -> None:
        self._asked[(s.market, s.setup)] = time.time()


def detect_setups(market, h1, h4, m15, btc: BtcContext, caution: list) -> list:
    """캔들 3종(마감된 봉만)으로 셋업을 찾는다. 테스트하기 쉽게 순수 함수로 분리."""
    lb = cfg.BREAKOUT_LOOKBACK
    if len(h1) < max(60, lb + 25) or len(h4) < 50 or len(m15) < 10:
        return []
    price = float(m15["close"].iloc[-1])
    a = float(ind.atr(h1, cfg.ATR_PERIOD).iloc[-1])
    if not price or not a or a != a:
        return []
    trend4 = ind.trend_label(h4)
    close1 = h1["close"]
    r1 = ind.rsi(close1)
    rsi_now = float(r1.iloc[-1])
    hi20 = float(h1["high"].iloc[-lb:].max())
    lo20 = float(h1["low"].iloc[-lb:].min())
    vol_hour = float(m15["volume"].iloc[-4:].sum())
    vol_avg = float(h1["volume"].iloc[-lb:].mean())
    vol_ratio = vol_hour / vol_avg if vol_avg else 0.0
    ret24 = ind.pct(price, float(close1.iloc[-24]))
    ret7d = ind.pct(price, float(h4["close"].iloc[-43])) if len(h4) >= 43 else 0.0
    e20, e50 = float(ind.ema(close1, 20).iloc[-1]), float(ind.ema(close1, 50).iloc[-1])

    recent = db.q("SELECT setup, pnl_r FROM positions WHERE market=? AND status='closed' AND qty>0 "
                  "ORDER BY closed_at DESC LIMIT 3", (market,))
    base = {
        "market": market,
        "trend_4h": trend4,
        "btc_regime": btc.regime,
        "btc_24h_pct": btc.ret24,
        "bb_pos_1h": round(float(ind.bb_pos(close1).iloc[-1]), 0),
        "dist_high20_pct": ind.pct(price, hi20),
        "dist_low20_pct": ind.pct(price, lo20),
        "rsi_1h": round(rsi_now, 1),
        "rsi_dir": ind.rsi_dir(r1),
        "macd_1h": ind.macd_state(close1),
        "vol_ratio_1h": round(vol_ratio, 2),
        "atr_pct_1h": round(a / price * 100, 2),
        "mom_15m": ind.momentum_label(m15),
        "ret_24h_pct": ret24,
        "rs_24h": round(ret24 - btc.ret24, 2),
        "rs_7d": round(ret7d - btc.ret7d, 2),
        "caution": caution,
        "recent_trades": [{"setup": r["setup"], "r": round(r["pnl_r"] or 0, 2)} for r in recent],
    }
    out = []

    # 돌파
    level = hi20
    if cfg.BREAKOUT_CONFIRM_1H:
        # 15분봉 꼬리 말고 '마감된 1시간봉'이 그 앞 20개 고점 위에서 끝나야 인정 + 그 1시간 거래량이 평소의 N배
        level = float(h1["high"].iloc[-lb - 1:-1].max())
        v_avg = float(h1["volume"].iloc[-lb - 1:-1].mean())
        brk = (float(close1.iloc[-1]) > level and v_avg > 0
               and float(h1["volume"].iloc[-1]) / v_avg >= cfg.BREAKOUT_VOL_MULT)
    else:
        brk = vol_ratio >= cfg.BREAKOUT_VOL_MULT
    trend_ok = trend4 == "UP" if cfg.BREAKOUT_REQUIRE_UPTREND else trend4 != "DOWN"
    if trend_ok and brk and price > level and (price - level) <= cfg.BREAKOUT_MAX_EXT_ATR * a:
        stop = level - cfg.BREAKOUT_STOP_ATR * a
        sp = (price - stop) / price
        if 0 < sp <= cfg.MAX_STOP_PCT:
            out.append(Setup(market, "BREAKOUT", "market", price, None, stop, sp, a,
                             cfg.BREAKOUT_TIME_STOP_H, base["rs_24h"],
                             dict(base, setup="BREAKOUT", stop_pct=round(sp * 100, 2))))

    # 눌림목 (돌파가 잡혔으면 생략)
    turning = base["mom_15m"] not in ("DOWN", "STRONG_DOWN") and base["rsi_dir"] != "DOWN"
    if (not out and cfg.PULLBACK_ENABLED and trend4 == "UP" and e20 > e50 and e50 <= price <= e20 + cfg.PULLBACK_NEAR_EMA_ATR * a
            and cfg.PULLBACK_RSI_MIN <= rsi_now <= cfg.PULLBACK_RSI_MAX
            and (turning or not cfg.PULLBACK_REQUIRE_TURN)):
        limit = price - cfg.PULLBACK_ENTRY_ATR * a
        stop = limit - cfg.PULLBACK_STOP_ATR * a
        sp = (limit - stop) / limit if limit > 0 else 1
        if 0 < sp <= cfg.MAX_STOP_PCT and stop > 0:
            out.append(Setup(market, "PULLBACK", "limit", price, limit, stop, sp, a,
                             cfg.PULLBACK_TIME_STOP_H, base["rs_24h"],
                             dict(base, setup="PULLBACK", stop_pct=round(sp * 100, 2))))
    return out
