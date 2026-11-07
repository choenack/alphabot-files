# -*- coding: utf-8 -*-
"""
messages.py — 텔레그램 알림 문구 (무엇을 샀는지, 얼마 벌었는지 한눈에)
=====================================================
매수 체결 · 수익 보호 시작 · 매도 · 보유 현황 · 오늘 매매 · 시작 알림 · 명령 도움말.
텔레그램 HTML 모드라서 코인 이름 같은 값은 html.escape로 감싼다.
"""
import html
import time
from datetime import datetime

import db
import risk
from config import cfg
from llm import reason_ko
from upbit import MARKET_NAMES

SETUP_KO = {"BREAKOUT": "돌파", "PULLBACK": "눌림목"}
CONV_KO = {"high": "확신 높음", "normal": "확신 보통"}
EXIT_KO = {"stop": "손절", "trail": "트레일링 익절", "time": "시간 손절", "manual": "수동 청산",
           "external": "외부 매도", "dust": "먼지 정리"}


# ── 표기 ────────────────────────────────────────────────────────────
def coin(m: str) -> str:
    sym = (m or "").split("-", 1)[-1]
    name = MARKET_NAMES.get(m)
    return html.escape(f"{name}({sym})" if name and name != m else sym)


def fp(x) -> str:
    """가격 표기(지수표기 없이)."""
    x = float(x or 0)
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.6g}"


def won(v) -> str:
    return f"{float(v or 0):,.0f}원"


def _sign(v: float, shown: str) -> str:
    """반올림하면 0인 값에는 부호를 붙이지 않는다(−0.0% 방지)."""
    if float(shown.replace(",", "")) == 0:
        return ""
    return "+" if v > 0 else "−"


def swon(v) -> str:
    v = float(v or 0)
    shown = f"{abs(v):,.0f}"
    return f"{_sign(v, shown)}{shown}원"


def spct(v, d: int = 1) -> str:
    v = float(v or 0)
    shown = f"{abs(v):.{d}f}"
    return f"{_sign(v, shown)}{shown}%"


def dur(sec: float) -> str:
    h = max(0.0, sec) / 3600
    if h < 1:
        return f"{h * 60:.0f}분"
    if h < 48:
        return f"{h:.1f}시간"
    return f"{h / 24:.1f}일"


def load_names(ub) -> None:
    """코인 한글 이름 채우기(시작할 때 한 번)."""
    if MARKET_NAMES:
        return
    try:
        MARKET_NAMES.update({m["market"]: m.get("korean_name") or m["market"] for m in ub.markets()
                             if m["market"].startswith("KRW-")})
    except Exception:
        pass


def today_start() -> float:
    now = datetime.now(risk.KST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _equity_line(prices: dict) -> str:
    s = risk.snapshot(prices)
    tot = s["equity"] - cfg.MAX_CAPITAL_KRW
    day0 = risk.day_start_equity(s["equity"])
    return (f"봇 자산 <b>{won(s['equity'])}</b> (시작 대비 {swon(tot)}, {spct(tot / cfg.MAX_CAPITAL_KRW * 100)})\n"
            f"오늘 {swon(s['equity'] - day0)} · 현금 {won(s['cash'])}")


# ── 매매 알림 ───────────────────────────────────────────────────────
def fill_text(p: dict) -> str:
    qty, entry, stop = float(p["qty"]), float(p["entry_price"]), float(p["stop_price"])
    amount = qty * entry
    loss = qty * (entry - stop) + float(p["entry_fee"] or 0) * 2
    held = len(db.positions("open", "pending_exit"))
    return (f"✅ <b>매수 체결 · {coin(p['market'])}</b>\n"
            f"{SETUP_KO.get(p['setup'], p['setup'])} · {CONV_KO.get(p['conviction'], p['conviction'])}\n"
            f"금액 <b>{won(amount)}</b> ({qty:,.6g}개 × {fp(entry)}원)\n"
            f"손절선 {fp(stop)}원 ({spct((stop / entry - 1) * 100)}) · 손절되면 약 {swon(-loss)}\n"
            f"근거: {html.escape(reason_ko(p.get('note') or ''))}\n"
            f"보유 {held}/{cfg.MAX_POSITIONS}개 · +{cfg.BE_TRIGGER_R:g}R({spct((float(p['r_value'] or 0) / entry) * 100)})부터 수익 보호")


def limit_wait_text(market: str, setup: str, conviction: str, reason: str, budget: float, price: float,
                    stop_pct: float) -> str:
    return (f"🕒 <b>지정가 매수 대기 · {coin(market)}</b>\n"
            f"{SETUP_KO.get(setup, setup)} · {CONV_KO.get(conviction, conviction)}\n"
            f"{fp(price)}원에 {won(budget)}어치 · {cfg.PULLBACK_EXPIRY_MIN}분 안에 안 사지면 자동 취소\n"
            f"사지면 손절 {spct(-stop_pct * 100)}\n"
            f"근거: {html.escape(reason_ko(reason))}")


def limit_cancel_text(p: dict) -> str:
    return f"↩️ 지정가 미체결 취소 · {coin(p['market'])} ({fp(p['limit_price'])}원까지 안 내려왔어요)"


def be_text(p: dict, price: float, new_stop: float) -> str:
    entry = float(p["entry_price"])
    return (f"🛡 <b>수익 보호 시작 · {coin(p['market'])}</b>\n"
            f"현재 {fp(price)}원 ({spct((price / entry - 1) * 100)}) → 손절선을 본전 {fp(new_stop)}원으로 올렸어요.\n"
            f"이제 손해 없이 고점 대비 {cfg.TRAIL_ATR:g} ATR 트레일링으로 따라가요.")


def exit_text(p: dict, exit_price: float, pnl: float, pnl_r: float, reason: str, prices: dict) -> str:
    entry = float(p["entry_price"])
    cost = float(p["qty"]) * entry
    icon = "🔵" if pnl >= 0 else "🔴"
    held = time.time() - float(p["opened_at"] or time.time())
    realized_today = (db.q1("SELECT COALESCE(SUM(pnl_krw),0) s FROM positions WHERE status='closed' AND closed_at>=?",
                            (today_start(),)) or {}).get("s", 0)
    return (f"{icon} <b>매도 완료 · {coin(p['market'])}</b> [{EXIT_KO.get(reason, reason)}]\n"
            f"<b>{swon(pnl)}</b> ({spct(pnl / cost * 100 if cost else 0, 2)}, {pnl_r:+.2f}R)\n"
            f"{fp(entry)}원 → {fp(exit_price)}원 · 보유 {dur(held)}\n"
            f"오늘 실현 손익 {swon(realized_today)}\n" + _equity_line(prices))


# ── 상태·요약 ───────────────────────────────────────────────────────
def holdings_lines(prices: dict) -> list[str]:
    out = []
    for p in db.active_positions():
        m = p["market"]
        px = prices.get(m)
        if p["status"] == "pending_entry":
            out.append(f"🕒 {coin(m)} 지정가 {fp(p['limit_price'])}원 대기 · {won(p['budget_krw'])}")
            continue
        entry, qty = float(p["entry_price"]), float(p["qty"])
        if not px:
            out.append(f"• {coin(m)} {won(qty * entry)} (현재가 조회 중)")
            continue
        pnl = qty * (px - entry) - float(p["entry_fee"] or 0)
        mode = "트레일링" if p["be_moved"] else "초기 손절"
        out.append(f"{'🟢' if pnl >= 0 else '🔻'} <b>{coin(m)}</b> {spct((px / entry - 1) * 100, 2)} ({swon(pnl)})\n"
                   f"   {fp(entry)} → {fp(px)}원 · 평가 {won(qty * px)} · 보유 {dur(time.time() - float(p['opened_at'] or time.time()))}\n"
                   f"   손절선 {fp(p['stop_price'])}원 ({spct((float(p['stop_price']) / px - 1) * 100)}) · {mode}")
    return out


def status_text(prices: dict, krw_free=None, title: str = "📊 보유 현황") -> str:
    lines = holdings_lines(prices)
    head = f"<b>{title}</b> {datetime.now(risk.KST):%m/%d %H:%M}"
    body = "\n".join(lines) if lines else "보유 중인 코인이 없어요."
    tail = _equity_line(prices)
    if krw_free is not None:
        tail += f" · 업비트 원화 {won(krw_free)}"
    flags = []
    if risk.halted():
        flags.append("⛔ 누적 손실 한도로 정지됨")
    if risk.entries_paused():
        flags.append("⏸ 신규 진입 일시정지 중")
    if time.time() < risk.paused_until():
        flags.append(f"🚨 BTC 급락으로 {datetime.fromtimestamp(risk.paused_until(), risk.KST):%H:%M}까지 진입 쉼")
    return "\n".join([head, body, "", tail] + flags)


def today_text(prices: dict) -> str:
    t0 = today_start()
    trades = db.closed_trades(t0)
    lines = [f"<b>📅 오늘 매매</b> {datetime.now(risk.KST):%m/%d}"]
    if not trades:
        lines.append("오늘은 청산된 매매가 없어요.")
    for t in trades:
        lines.append(f"{'🔵' if (t['pnl_krw'] or 0) >= 0 else '🔴'} {coin(t['market'])} {swon(t['pnl_krw'])} "
                     f"({(t['pnl_r'] or 0):+.2f}R) · {EXIT_KO.get(t['exit_reason'], t['exit_reason'] or '')} · "
                     f"{datetime.fromtimestamp(t['closed_at'], risk.KST):%H:%M}")
    if trades:
        wins = sum(1 for t in trades if (t["pnl_krw"] or 0) > 0)
        lines.append(f"합계 <b>{swon(sum(t['pnl_krw'] or 0 for t in trades))}</b> · {len(trades)}회 중 {wins}승")
    n_dec = (db.q1("SELECT COUNT(*) n, SUM(action='ENTER') e FROM decisions WHERE ts>=? AND is_shadow=0", (t0,)) or {})
    llm_usd = (db.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM llm_calls WHERE ts>=?", (t0,)) or {}).get("s", 0)
    lines.append(f"LLM 판단 {n_dec.get('n') or 0}번(진입 {n_dec.get('e') or 0}) · 비용 {won(float(llm_usd or 0) * cfg.FX_KRW_PER_USD)}")
    hold = holdings_lines(prices)
    if hold:
        lines += ["", "<b>지금 보유</b>"] + hold
    lines += ["", _equity_line(prices)]
    return "\n".join(lines)


def start_text(krw: float, prices: dict, restarted: bool = False) -> str:
    head = "🔄 <b>엔진 재시작 완료</b>" if restarted else "▶️ <b>coin ai 시작</b>"
    hold = holdings_lines(prices)
    return "\n".join([head, f"운용금 {won(risk.allocation())} · 업비트 원화 {won(krw)} · 감시 {cfg.UNIVERSE_SIZE}개",
                      f"판단 {cfg.DEEPSEEK_MODEL} · 15분마다 스캔"]
                     + (["", "<b>보유 중</b>"] + hold if hold else ["보유 중인 코인 없음"])
                     + ["", "명령: /status 상태 · /today 오늘 · /report 성과 · /pause 멈춤 · /resume 재개"])


def help_text() -> str:
    return ("<b>coin ai 명령</b>\n"
            "/status — 지금 보유 코인, 수익률, 손절선, 자산\n"
            "/today — 오늘 매매와 손익\n"
            "/report — 최근 7일 성과\n"
            "/pause — 신규 진입 멈춤(보유분 손절 감시는 계속)\n"
            "/resume — 신규 진입 재개")
