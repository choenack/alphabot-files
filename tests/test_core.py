# -*- coding: utf-8 -*-
"""핵심 로직 테스트 — 인터넷·API 키 없이 돌아간다.  실행: python -m pytest -q"""
import hashlib
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import evolve  # noqa: E402
import indicators as ind  # noqa: E402
import llm  # noqa: E402
import risk  # noqa: E402
from config import cfg  # noqa: E402
from strategy import BtcContext, Setup, detect_setups  # noqa: E402
from trader import Trader, update_stops  # noqa: E402
from upbit import Upbit, fill_info  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.init(":memory:")
    yield


def _df(closes, start, step_h, vol=100.0, spread=0.004):
    ts = [start + timedelta(hours=step_h * i) for i in range(len(closes))]
    c = np.array(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": ts, "open": o, "high": np.maximum(o, c) * (1 + spread),
                         "low": np.minimum(o, c) * (1 - spread), "close": c,
                         "volume": np.full(len(c), vol), "value": c * vol})


# ── 지표 ────────────────────────────────────────────────────────────
def test_indicators_basic():
    up = pd.Series(np.linspace(100, 200, 100))
    assert ind.rsi(up).iloc[-1] == 100.0
    df = _df(np.linspace(100, 200, 100), datetime(2026, 1, 1, tzinfo=timezone.utc), 4)
    assert ind.trend_label(df) == "UP"
    assert ind.atr(df).iloc[-1] > 0
    assert ind.pct(110, 100) == 10.0


# ── 업비트 클라이언트 ────────────────────────────────────────────────
def test_round_price_and_volume():
    ub = Upbit("a", "b")
    ub._ticks["KRW-DOGE"] = "0.1"
    ub._ticks["KRW-BTC"] = "1000"
    assert ub.round_price("KRW-DOGE", 251.37) == "251.3"
    assert ub.round_price("KRW-BTC", 102_555_999) == "102555000"
    assert ub.round_volume(0.123456789123) == "0.12345678"
    assert ub.round_volume(1500) == "1500"


def test_jwt_query_hash():
    ub = Upbit("access", "secret")
    h = ub._headers({"market": "KRW-BTC", "side": "bid", "states[]": ["wait", "watch"]})
    tok = h["Authorization"].split(" ", 1)[1]
    payload = jwt.decode(tok, "secret", algorithms=["HS256"])
    qs = "market=KRW-BTC&side=bid&states[]=wait&states[]=watch"
    assert payload["query_hash"] == hashlib.sha512(qs.encode()).hexdigest()
    assert payload["access_key"] == "access"


def test_fill_info_market_buy_cancel_state():
    o = {"state": "cancel", "executed_volume": "0.5", "paid_fee": "10",
         "trades": [{"volume": "0.2", "funds": "8000"}, {"volume": "0.3", "funds": "12300"}]}
    fi = fill_info(o)
    assert fi["final"] and fi["executed_volume"] == 0.5
    assert fi["avg_price"] == pytest.approx(40600)


# ── 손절선 ──────────────────────────────────────────────────────────
def _pos(**kw):
    p = dict(entry_price=100.0, r_value=4.0, peak_price=100.0, stop_price=96.0, be_moved=0,
             atr_abs=2.0, opened_at=time.time(), time_stop_h=24)
    p.update(kw)
    return p


def test_update_stops_breakeven_and_trailing():
    p = _pos()
    upd, r = update_stops(p, 103)
    assert r is None and "be_moved" not in upd
    upd, r = update_stops(p, 104)                       # +1R → 본전
    assert upd["be_moved"] == 1 and upd["stop_price"] == pytest.approx(100 * (1 + 2 * cfg.FEE_RATE + 0.001))
    p.update(upd)
    upd, r = update_stops(p, 110)                       # 고점 110 → 110 - 3*2 = 104
    assert upd["stop_price"] == pytest.approx(104) and r is None
    p.update(upd)
    upd, r = update_stops(p, 103.9)
    assert r == "trail"


def test_update_stops_stop_and_time():
    assert update_stops(_pos(), 95.9)[1] == "stop"
    old = _pos(opened_at=time.time() - 25 * 3600)
    assert update_stops(old, 101)[1] == "time"
    assert update_stops(_pos(opened_at=time.time() - 25 * 3600, be_moved=1, stop_price=100.3), 105)[1] is None


# ── 비중 ────────────────────────────────────────────────────────────
def _setup(stop_pct=0.04, rs24=1.0, kind="BREAKOUT", rs7=1.0):
    return Setup("KRW-TEST", kind, "market", 100.0, None, 96.0, stop_pct, 2.0, 12, rs24, {"rs_7d": rs7})


def test_entry_size_caps_and_streak():
    size, why = risk.entry_size(_setup(), "high", "UP", {}, 1_000_000)
    assert why == "" and size == int(cfg.MAX_CAPITAL_KRW * cfg.MAX_POSITION_PCT)   # 3%/4% = 75% → 45% 상한
    db.put("loss_streak", 3)
    size2, _ = risk.entry_size(_setup(), "normal", "UP", {}, 1_000_000)
    assert size2 == int(cfg.MAX_CAPITAL_KRW * cfg.RISK_NORMAL * 0.5 / 0.04)


def test_entry_size_regime_down():
    assert risk.entry_size(_setup(rs24=1.0), "high", "DOWN", {}, 1e6)[0] == 0
    assert risk.entry_size(_setup(rs24=4.0, rs7=-2), "high", "DOWN", {}, 1e6)[0] == 0
    assert risk.entry_size(_setup(rs24=4.0, rs7=2, kind="PULLBACK"), "high", "DOWN", {}, 1e6)[0] > 0


def test_entry_size_limits():
    assert risk.entry_size(_setup(), "high", "UP", {}, 3000)[0] == 0          # 잔고 부족
    db.put("halted", True)
    assert risk.entry_size(_setup(), "high", "UP", {}, 1e6)[1].startswith("봇 정지")


# ── LLM 응답 정리 ───────────────────────────────────────────────────
def test_llm_parse_and_sanitize():
    assert llm.sanitize(llm.parse_json('```json\n{"action":"enter","conviction":"HIGH","reason":"RS_STRONG+FOO+BTC_UP"}```')) \
        == {"action": "ENTER", "conviction": "high", "reason": "RS_STRONG+BTC_UP"}
    assert llm.sanitize(llm.parse_json("blah {\"action\":\"SKIP\"} blah"))["action"] == "SKIP"
    assert llm.sanitize({"action": "BUY"}) is None
    assert llm.sanitize(llm.parse_json("")) is None


# ── 셋업 감지 ───────────────────────────────────────────────────────
def test_detect_breakout():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    h4 = _df(np.linspace(80, 100, 120), now - timedelta(hours=4 * 120), 4)
    base = list(np.linspace(90, 100, 170)) + [100 + 0.3 * np.sin(i) for i in range(30)]
    h1 = _df(base, now - timedelta(hours=200), 1, spread=0.003)
    m15 = _df([100.2] * 36 + [100.4, 100.6, 100.8, 101.0], now - timedelta(minutes=15 * 40), 0.25, spread=0.002)
    m15.loc[m15.index[-4:], "volume"] = 60.0                       # 최근 1시간 240 ≥ 평소 100 × 2
    ss = detect_setups("KRW-TEST", h1, h4, m15, BtcContext("UP", 0.5, 1.0, 0), [])
    assert [s.setup for s in ss] == ["BREAKOUT"]
    s = ss[0]
    assert s.stop_price < s.price and 0 < s.stop_pct <= cfg.MAX_STOP_PCT
    assert s.features["vol_ratio_1h"] >= 2 and s.features["setup"] == "BREAKOUT"


def test_detect_nothing_on_weak_volume():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    h4 = _df(np.linspace(80, 100, 120), now - timedelta(hours=4 * 120), 4)
    h1 = _df(list(np.linspace(90, 100, 170)) + [100] * 30, now - timedelta(hours=200), 1)
    m15 = _df([100.2] * 36 + [100.6, 101.0, 101.4, 101.8], now - timedelta(minutes=600), 0.25)
    assert all(s.setup != "BREAKOUT" for s in detect_setups("KRW-TEST", h1, h4, m15, BtcContext(), []))


# ── 주문 흐름(가짜 거래소) ───────────────────────────────────────────
class FakeUpbit:
    def __init__(self):
        self.krw, self.coin, self.px = 100_000.0, 0.0, 100.0
        self.orders, self.n = {}, 0

    def balance(self, cur):
        return (self.krw, 0.0) if cur == "KRW" else (self.coin, 0.0)

    def accounts(self):
        return [{"currency": "KRW", "balance": self.krw, "locked": 0}, {"currency": "TEST", "balance": self.coin, "locked": 0}]

    def prices(self, markets):
        return {m: self.px for m in markets}

    def new_identifier(self, p):
        self.n += 1
        return f"id{self.n}"

    def round_price(self, m, p):
        return f"{p:.1f}"

    def _done(self, ident, side, vol, price):
        u = f"u{len(self.orders)}"
        funds = vol * price
        fee = funds * cfg.FEE_RATE
        if side == "bid":
            self.krw -= funds + fee
            self.coin += vol
        else:
            self.krw += funds - fee
            self.coin -= vol
        self.orders[u] = {"uuid": u, "identifier": ident, "state": "done", "executed_volume": vol,
                          "paid_fee": fee, "trades": [{"volume": vol, "funds": funds}]}
        return {"uuid": u}

    def buy_market(self, m, krw, ident):
        return self._done(ident, "bid", krw / self.px, self.px)

    def sell_market(self, m, vol, ident):
        return self._done(ident, "ask", vol, self.px)

    def order(self, uuid=None, identifier=None):
        if uuid:
            return self.orders[uuid]
        return next(o for o in self.orders.values() if o["identifier"] == identifier)


def test_trader_enter_and_exit_flow():
    ub = FakeUpbit()
    t = Trader(ub)
    t.regime = "UP"
    s = _setup(stop_pct=0.04)
    t._enter(s, {"conviction": "high", "reason": "TEST", "decision_id": None})
    t.poll()
    p = db.positions("open")[0]
    assert p["qty"] == pytest.approx(450) and p["entry_price"] == 100 and p["r_value"] == pytest.approx(4)
    ub.px = 110.0
    t._exit(p["id"], "trail")
    t.poll()
    c = db.position(p["id"])
    assert c["status"] == "closed"
    expected = 450 * 110 * (1 - cfg.FEE_RATE) - 450 * 100 * (1 + cfg.FEE_RATE)
    assert c["pnl_krw"] == pytest.approx(expected)
    assert risk.allocation() == pytest.approx(cfg.MAX_CAPITAL_KRW + expected)
    assert db.get("loss_streak") == 0


def test_trader_skips_duplicate_market():
    ub = FakeUpbit()
    t = Trader(ub)
    t.regime = "UP"
    t._enter(_setup(), {"conviction": "high", "reason": "", "decision_id": None})
    t._enter(_setup(), {"conviction": "high", "reason": "", "decision_id": None})
    assert len(db.active_positions()) == 1


# ── 자가진화 점수 ───────────────────────────────────────────────────
def test_evolve_score():
    base = {"stop_pct": 0.04, "fwd_24h": 8.0, "fwd_24h_min": -1.0, "action": "ENTER"}
    assert evolve.score(base) == 2.0
    assert evolve.score(dict(base, fwd_24h_min=-4.5)) == -1.0
    assert evolve.score(dict(base, fwd_24h=40.0)) == 3.0
    assert evolve.score(dict(base, action="SKIP")) == 0.0
    assert evolve.score(dict(base, fwd_24h=None)) is None
