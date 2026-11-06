# -*- coding: utf-8 -*-
"""
risk.py — 운용금 계산 + 진입 크기 + 손실 한도
=====================================================
돈 계산(봇이 쓰는 돈만)
  · 운용금(allocation) = MAX_CAPITAL_KRW + 봇의 누적 실현손익   → 벌면 커지고 잃으면 작아진다(복리)
  · 봇 현금 = 운용금 - 보유 포지션 원가 - 진입 대기 주문에 묶인 돈
  · 봇 자산(equity) = 봇 현금 + 보유 포지션 평가액
  계좌에 돈이 더 있어도 봇은 이 범위 안에서만 쓰고, 사용자가 직접 산 코인은 건드리지 않는다.
진입 크기
  · 위험금 = 자산 × (확신 high 3% / normal 1.5%) × BTC 상태 배수 × (연속 3패면 0.5)
  · 크기 = 위험금 ÷ 손절 거리, 단 자산의 45% / 봇 현금 / 실제 원화 잔고를 넘지 않게
멈춤 장치
  · 하루 -8% → 그날 신규 진입 중지 · 시작금 대비 -25% → 봇 정지(수동 해제)
  · BTC 급락 → 일정 시간 신규 진입 중지(monitor가 설정)
"""
import time
from datetime import datetime, timedelta, timezone

import db
from config import cfg

KST = timezone(timedelta(hours=9))


def kst_day() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def realized_pnl() -> float:
    r = db.q1("SELECT COALESCE(SUM(pnl_krw), 0) AS s FROM positions WHERE status='closed'")
    return float(r["s"] or 0)


def allocation() -> float:
    return cfg.MAX_CAPITAL_KRW + realized_pnl()


def _cost(p: dict) -> float:
    if p["status"] == "pending_entry":
        return float(p["budget_krw"] or 0) * (1 + cfg.FEE_RATE)
    return float(p["qty"] or 0) * float(p["entry_price"] or 0) + float(p["entry_fee"] or 0)


def snapshot(prices: dict) -> dict:
    """현재 봇 자산 상태. prices: {market: 현재가} (보유 코인 가격)."""
    act = db.active_positions()
    invested = sum(_cost(p) for p in act)
    value = 0.0
    for p in act:
        if p["status"] == "pending_entry":
            value += _cost(p)
        else:
            px = prices.get(p["market"]) or p["entry_price"] or 0
            value += float(p["qty"] or 0) * float(px)
    alloc = allocation()
    cash = alloc - invested
    return {"allocation": alloc, "cash": cash, "invested": invested,
            "equity": cash + value, "count": len(act)}


def day_start_equity(equity_now: float) -> float:
    """KST 기준 하루 시작 자산. 날이 바뀐 뒤 처음 불릴 때의 자산으로 정한다."""
    today = kst_day()
    if db.get("day_key") != today:
        db.put("day_key", today)
        db.put("day_start_equity", equity_now)
        db.put("daily_limit_hit", False)
        return equity_now
    return float(db.get("day_start_equity", equity_now))


def halted() -> bool:
    return bool(db.get("halted", False))


def paused_until() -> float:
    return float(db.get("pause_until", 0) or 0)


def loss_streak() -> int:
    return int(db.get("loss_streak", 0) or 0)


def on_trade_closed(pnl_krw: float) -> None:
    db.put("loss_streak", loss_streak() + 1 if pnl_krw < 0 else 0)


def regime_mult(regime: str, setup) -> tuple[float, str]:
    if regime == "UP":
        return cfg.REGIME_MULT_UP, ""
    if regime == "SIDE":
        return cfg.REGIME_MULT_SIDE, ""
    # BTC 하락장: BTC와 상관없이 오르는 코인(24h·7일 모두 BTC보다 강함)은 돌파·눌림목 둘 다 허용
    rs7 = float((setup.features or {}).get("rs_7d", 0) or 0)
    if setup.rs24 >= cfg.DOWN_REGIME_MIN_RS and rs7 >= cfg.DOWN_REGIME_MIN_RS7:
        return cfg.REGIME_MULT_DOWN, ""
    return 0.0, "BTC 하락장(BTC보다 강한 코인만 허용)"


def entry_size(setup, conviction: str, regime: str, prices: dict, krw_free: float) -> tuple[float, str]:
    """반환: (주문 금액, 거절 사유). 금액이 0이면 진입하지 않는다."""
    if halted():
        return 0.0, "봇 정지 상태(누적 손실 한도)"
    if time.time() < paused_until():
        return 0.0, "BTC 급락으로 일시 정지 중"
    s = snapshot(prices)
    if s["count"] >= cfg.MAX_POSITIONS:
        return 0.0, f"동시 보유 한도({cfg.MAX_POSITIONS}개)"
    if s["equity"] <= day_start_equity(s["equity"]) * (1 - cfg.DAILY_LOSS_LIMIT):
        return 0.0, "하루 손실 한도 도달"
    mult, why = regime_mult(regime, setup)
    if mult <= 0:
        return 0.0, why
    risk_pct = cfg.RISK_HIGH if conviction == "high" else cfg.RISK_NORMAL
    risk_pct *= mult
    if loss_streak() >= cfg.LOSS_STREAK_HALVE:
        risk_pct *= 0.5
    size = s["equity"] * risk_pct / max(setup.stop_pct, 0.005)
    size = min(size, s["equity"] * cfg.MAX_POSITION_PCT, s["cash"], krw_free / (1 + cfg.FEE_RATE) * 0.998)
    # 손절 뒤에도 최소 주문 금액(5천 원) 이상이 남아야 팔 수 있다
    if size < cfg.MIN_ORDER_KRW * 1.3:
        return 0.0, f"주문 금액이 너무 작음({size:,.0f}원)"
    return float(int(size)), ""


def check_total_stop(equity: float) -> bool:
    """기준금(처음엔 시작금, 정지 해제 후엔 해제 시점 운용금) 대비 -25% 아래면 정지시키고 True."""
    base = float(db.get("stop_base", cfg.MAX_CAPITAL_KRW) or cfg.MAX_CAPITAL_KRW)
    if not halted() and equity <= base * (1 - cfg.TOTAL_STOP_DD):
        db.put("halted", True)
        return True
    return False


def reset_halt() -> None:
    db.put("halted", False)
    db.put("pause_until", 0)
    db.put("stop_base", allocation())
