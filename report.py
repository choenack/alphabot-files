# -*- coding: utf-8 -*-
"""
report.py — 성과 점검 (매주 볼 숫자)
=====================================================
python report.py          → 최근 7일
python report.py 30       → 최근 30일
python report.py all      → 전체
승률 · 평균 이익/손실(R) · 총이익÷총손실 · 최대 낙폭 · BTC 보유 대비 · 수수료 · LLM 비용
"""
import sys
import time

import db
import risk
from config import cfg


def stats(days: float | None = 7) -> dict:
    since = time.time() - days * 86400 if days else 0
    trades = db.closed_trades(since)
    wins = [t for t in trades if (t["pnl_krw"] or 0) > 0]
    losses = [t for t in trades if (t["pnl_krw"] or 0) <= 0]
    gross_win = sum(t["pnl_krw"] for t in wins)
    gross_loss = -sum(t["pnl_krw"] or 0 for t in losses)
    fees = sum((t["entry_fee"] or 0) + (t["exit_fee"] or 0) for t in trades)
    eq = db.q("SELECT * FROM equity WHERE ts>=? ORDER BY ts", (since,))
    mdd, peak = 0.0, 0.0
    for e in eq:
        peak = max(peak, e["equity"])
        if peak:
            mdd = max(mdd, 1 - e["equity"] / peak)
    btc = None
    if len(eq) >= 2 and eq[0]["btc_price"] and eq[-1]["btc_price"]:
        btc = (eq[-1]["btc_price"] / eq[0]["btc_price"] - 1) * 100
    bot = (eq[-1]["equity"] / eq[0]["equity"] - 1) * 100 if len(eq) >= 2 and eq[0]["equity"] else None
    llm_usd = (db.q1("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_calls WHERE ts>=?", (since,)) or {}).get("s", 0)
    by_setup = {}
    for t in trades:
        b = by_setup.setdefault(t["setup"], [0, 0.0])
        b[0] += 1
        b[1] += t["pnl_krw"] or 0
    return {
        "days": days, "n": len(trades), "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "avg_win_r": sum(t["pnl_r"] or 0 for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(t["pnl_r"] or 0 for t in losses) / len(losses) if losses else 0.0,
        "pf": gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "pnl": sum(t["pnl_krw"] or 0 for t in trades), "fees": fees,
        "mdd": mdd * 100, "bot_ret": bot, "btc_ret": btc,
        "llm_krw": float(llm_usd or 0) * cfg.FX_KRW_PER_USD, "by_setup": by_setup,
        "allocation": risk.allocation(),
    }


def text(days: float | None = 7) -> str:
    s = stats(days)
    span = f"최근 {days:g}일" if days else "전체"
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    lines = [
        f"📊 <b>성과 ({span})</b>",
        f"매매 {s['n']}회 · 승률 {s['win_rate']:.0f}% · 총이익÷총손실 {pf}",
        f"평균 이익 {s['avg_win_r']:+.2f}R · 평균 손실 {s['avg_loss_r']:+.2f}R",
        f"실현 손익 {s['pnl']:+,.0f}원 · 수수료 {s['fees']:,.0f}원 · LLM {s['llm_krw']:,.0f}원",
        f"최대 낙폭 {s['mdd']:.1f}%",
    ]
    if s["bot_ret"] is not None and s["btc_ret"] is not None:
        lines.append(f"봇 {s['bot_ret']:+.1f}% vs BTC 보유 {s['btc_ret']:+.1f}%")
    for k, (n, pnl) in s["by_setup"].items():
        lines.append(f"· {k}: {n}회 {pnl:+,.0f}원")
    lines.append(f"현재 운용금 {s['allocation']:,.0f}원 (시작 {cfg.MAX_CAPITAL_KRW:,.0f}원)")
    return "\n".join(lines)


if __name__ == "__main__":
    db.init()
    arg = sys.argv[1] if len(sys.argv) > 1 else "7"
    d = None if arg == "all" else float(arg)
    print(text(d).replace("<b>", "").replace("</b>", ""))
