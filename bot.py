# -*- coding: utf-8 -*-
"""
bot.py — 매매 엔진 (스레드 4개)
=====================================================
  · 스캔:   15분봉 마감마다 감시 대상 30개 셋업 검사 → 셋업만 LLM 판단 → ENTER면 주문 스레드로
  · 감시:   2초마다 보유 코인 가격 확인 → 손절·본전·트레일링·시간 손절, 1분마다 BTC 급락 확인
  · 주문:   진입·청산·체결 확인을 한 줄로 차례대로
  · 정비:   1시간마다 사후 결과 채우기, 자가진화·섀도 비교 일정 확인
start()/shutdown()은 바로 돌아온다(대시보드가 엔진을 켜고 끌 수 있게). state는 대시보드 표시용.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

import db
import evolve
import llm
import notify
import report
import risk
from config import cfg
from strategy import Scanner
from trader import Trader, update_stops
from upbit import Upbit, UpbitError

log = logging.getLogger("bot")


class Bot:
    def __init__(self):
        self.ub = Upbit()
        self.scanner = Scanner(self.ub)
        self.trader = Trader(self.ub)
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self._exit_sent: dict[int, float] = {}
        self._last_kill = 0.0
        self._last_snap = 0.0
        self.state = {"running": False, "started_at": None, "last_scan": None, "next_scan": None,
                      "btc": None, "universe": [], "setups": [], "krw_free": None, "prices": {}}

    # ── 스캔 ────────────────────────────────────────────────────────
    def scan_once(self, dry: bool = False) -> list:
        if risk.halted():
            log.warning("봇 정지 상태 — 신규 진입 안 함 (대시보드의 '정지 해제'로 다시 켬)")
            return []
        universe = self.scanner.refresh_universe()
        btc = self.scanner.btc_context()
        self.trader.regime = btc.regime
        self.state.update(universe=list(universe), btc={"regime": btc.regime, "ret24": btc.ret24,
                                                         "ret7d": btc.ret7d, "price": btc.price})
        active = {p["market"] for p in db.active_positions()}
        setups = []
        for m in universe:
            if m in active or time.time() - db.last_exit_ts(m) < cfg.REENTRY_COOLDOWN_SEC:
                continue
            try:
                for s in self.scanner.analyze(m, btc):
                    if dry or not self.scanner.recently_asked(s):
                        setups.append(s)
            except Exception as e:
                log.warning("분석 실패 %s: %s", m, e)
        # LLM 비용 아끼기: 어차피 못 사는 셋업은 묻지 않는다
        setups = [s for s in setups if risk.regime_mult(btc.regime, s)[0] > 0]
        log.info("스캔: 감시 %d개 · BTC %s(24h %+.1f%%) · 셋업 %d개 %s", len(universe), btc.regime, btc.ret24,
                 len(setups), [f"{s.market}:{s.setup}" for s in setups])
        summary = [{"market": s.market, "setup": s.setup, "stop_pct": round(s.stop_pct * 100, 2),
                    "rs24": s.rs24, "vol_ratio": s.features.get("vol_ratio_1h"),
                    "trend_4h": s.features.get("trend_4h"), "action": None} for s in setups]
        self.state.update(last_scan=time.time(), setups=summary)
        if dry or not setups:
            return setups
        if risk.entries_paused() or time.time() < risk.paused_until() or len(active) >= cfg.MAX_POSITIONS:
            log.info("지금은 진입 불가(일시정지 또는 보유 한도) — LLM 판단 생략")
            return setups
        ver, text = llm.active_prompt()
        for s in setups:
            self.scanner.mark_asked(s)
        with ThreadPoolExecutor(max_workers=cfg.LLM_PARALLEL) as ex:
            results = list(ex.map(lambda s: (s, llm.judge(s, ver, text)), setups))
        for (s, d), row in zip(results, summary):
            row["action"] = d["action"]
            log.info("판단 %s %s → %s/%s %s%s", s.market, s.setup, d["action"], d["conviction"], d["reason"],
                     f" (실패: {d['error']})" if d.get("error") else "")
        enters = [(s, d) for s, d in results if d["action"] == "ENTER"]
        enters.sort(key=lambda x: (x[1]["conviction"] != "high", -x[0].rs24))
        for s, d in enters:
            self.trader.submit("enter", setup=s, decision=d)
        sh = llm.shadow_prompt()
        if sh:   # 새 프롬프트 비교용 판단(주문 X). 매매 판단이 끝난 뒤에 돌린다.
            with ThreadPoolExecutor(max_workers=cfg.LLM_PARALLEL) as ex:
                list(ex.map(lambda s: llm.judge(s, sh[0], sh[1], shadow=True), setups))
        return setups

    def scan_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.scan_once()
            except UpbitError as e:
                notify.error(f"스캔 중 업비트 오류: {e}", key="scan-upbit")
            except Exception as e:
                log.exception("스캔 오류")
                notify.error(f"스캔 오류: {e}", key="scan")
            step = cfg.SCAN_EVERY_MIN * 60
            now = time.time()
            nxt = (now // step + 1) * step + cfg.SCAN_DELAY_SEC
            self.state["next_scan"] = nxt
            self.stop.wait(max(5.0, nxt - now))

    # ── 감시 ────────────────────────────────────────────────────────
    def monitor_tick(self) -> None:
        held = db.positions("open", "pending_exit", "pending_entry")
        markets = sorted({p["market"] for p in held} | {"KRW-BTC"})
        prices = self.ub.prices(markets)
        self.state["prices"] = prices
        for p in held:
            if p["status"] != "open":
                continue
            px = prices.get(p["market"])
            if not px:
                continue
            upd, reason = update_stops(p, px)
            if upd:
                db.update_position(p["id"], **upd)
            if reason and time.time() - self._exit_sent.get(p["id"], 0) > 30:
                self._exit_sent[p["id"]] = time.time()
                log.info("청산 신호 %s %s @ %s", p["market"], reason, px)
                self.trader.submit("exit", pid=p["id"], reason=reason)
        now = time.time()
        if now - self._last_kill >= 60:
            self._last_kill = now
            self.kill_check()
        if now - self._last_snap >= 600:
            self._last_snap = now
            self.equity_check(prices)

    def kill_check(self) -> None:
        c = self.ub.candles("KRW-BTC", "minutes/5", 13, closed_only=False)
        if len(c) < 3:
            return
        hi, px = float(c["high"].max()), float(c["close"].iloc[-1])
        drop = 1 - px / hi
        if drop >= cfg.KILL_BTC_DROP_1H and time.time() >= risk.paused_until():
            db.put("pause_until", time.time() + cfg.KILL_PAUSE_SEC)
            self.trader.submit("cancel_buys", reason="BTC 급락")
            notify.send(f"🚨 <b>BTC 급락</b> 1시간 고점 대비 -{drop * 100:.1f}%\n"
                        f"대기 매수 취소, {cfg.KILL_PAUSE_SEC / 3600:g}시간 신규 진입 중지 (보유분 손절은 계속 감시)")

    def equity_check(self, prices: dict) -> None:
        try:
            self.state["krw_free"] = self.ub.balance("KRW")[0]
        except Exception as e:
            log.warning("원화 잔고 조회 실패: %s", e)
        s = risk.snapshot(prices)
        db.snap_equity(s["equity"], s["cash"], s["invested"], prices.get("KRW-BTC"))
        day0 = risk.day_start_equity(s["equity"])
        if s["equity"] <= day0 * (1 - cfg.DAILY_LOSS_LIMIT) and not db.get("daily_limit_hit"):
            db.put("daily_limit_hit", True)
            notify.send(f"🛑 하루 손실 한도 도달({(s['equity'] / day0 - 1) * 100:.1f}%) — 오늘은 신규 진입 중지")
        if risk.check_total_stop(s["equity"]):
            self.trader.submit("cancel_buys", reason="봇 정지")
            notify.send(f"⛔ <b>봇 정지</b> 자산 {s['equity']:,.0f}원 (기준금 대비 -{cfg.TOTAL_STOP_DD * 100:.0f}% 도달)\n"
                        "신규 진입을 멈췄어요. 보유분 손절 감시는 계속합니다. 대시보드에서 '정지 해제'로 다시 켜요.")
        now = datetime.now(risk.KST)
        if now.hour == 23 and now.minute >= 50 and db.get("daily_report_day") != risk.kst_day():
            db.put("daily_report_day", risk.kst_day())
            notify.send(report.text(7 if now.weekday() == 6 else 1))

    def monitor_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.monitor_tick()
            except UpbitError as e:
                if e.ip_blocked:
                    self.trader._upbit_error(e)
                else:
                    log.warning("감시 중 업비트 오류: %s", e)
            except Exception as e:
                log.warning("감시 오류: %s", e)
            self.stop.wait(cfg.MONITOR_SEC)

    # ── 정비 ────────────────────────────────────────────────────────
    def maintenance_loop(self) -> None:
        self.stop.wait(60)
        while not self.stop.is_set():
            try:
                n = evolve.backfill(self.ub)
                if n:
                    log.info("사후 결과 %d개 채움", n)
                for msg in (evolve.maybe_conclude_shadow(), evolve.maybe_run()):
                    if msg:
                        log.info("자가진화: %s", msg)
            except Exception as e:
                log.exception("정비 오류")
                notify.error(f"정비 작업 오류: {e}", key="maint")
            self.stop.wait(3600)

    # ── 켜기/끄기 ───────────────────────────────────────────────────
    def start(self) -> tuple[bool, str]:
        if not self.ub.has_keys:
            return False, "업비트 API 키가 없어요 — 설정 탭에서 입력하세요"
        if not cfg.DEEPSEEK_API_KEY and not cfg.ANTHROPIC_API_KEY:
            return False, "LLM 키(DeepSeek)가 없어요 — 설정 탭에서 입력하세요"
        try:
            krw, _ = self.ub.balance("KRW")
        except UpbitError as e:
            if e.ip_blocked:
                return False, "업비트가 이 PC의 IP를 거부했어요 — 업비트 Open API 관리에서 허용 IP를 등록하세요"
            return False, f"업비트 연결 실패({e.name}) — API 키와 권한(자산조회·주문조회·주문하기)을 확인하세요"
        except requests.exceptions.RequestException:
            return False, "업비트에 연결할 수 없어요 — 인터넷 연결을 확인하세요"
        self.state["krw_free"] = krw
        self.trader.reconcile()
        s = risk.snapshot({})
        notify.send(f"▶️ <b>시작</b> 운용금 {s['allocation']:,.0f}원 · 원화 잔고 {krw:,.0f}원\n"
                    f"보유/대기 {s['count']}개 · 판단 {cfg.DEEPSEEK_MODEL} · 감시 {cfg.UNIVERSE_SIZE}개")
        self.stop.clear()
        self.threads = [threading.Thread(target=self.trader.run, args=(self.stop,), name="trader", daemon=True),
                        threading.Thread(target=self.monitor_loop, name="monitor", daemon=True),
                        threading.Thread(target=self.scan_loop, name="scan", daemon=True),
                        threading.Thread(target=self.maintenance_loop, name="maint", daemon=True)]
        for t in self.threads:
            t.start()
        self.state.update(running=True, started_at=time.time())
        return True, "실행 중"

    def alive(self) -> bool:
        return bool(self.threads) and all(t.is_alive() for t in self.threads)

    def shutdown(self, reason: str = "종료") -> None:
        if not self.state["running"]:
            return
        self.stop.set()
        for t in self.threads:
            t.join(timeout=10)
        try:
            self.trader._cancel_entries(reason)   # 꺼져 있는 동안 체결되지 않게
        except Exception as e:
            log.warning("종료 시 대기 주문 취소 실패: %s", e)
        self.state["running"] = False
        n = len(db.positions("open"))
        notify.send(f"⏹ {reason} — 보유 {n}개는 다시 켤 때까지 손절 감시가 멈춥니다" if n else f"⏹ {reason}")

    # ── 점검(주문 안 함) ────────────────────────────────────────────
    def check(self) -> list[str]:
        out = [f"운용 상한 {cfg.MAX_CAPITAL_KRW:,.0f}원 · 감시 {cfg.UNIVERSE_SIZE}개 · 동시 보유 {cfg.MAX_POSITIONS}개 · "
               f"위험 high {cfg.RISK_HIGH:.1%}/normal {cfg.RISK_NORMAL:.1%} · 하루 한도 -{cfg.DAILY_LOSS_LIMIT:.0%}",
               "키: " + ", ".join(f"{k} {'있음' if v else '없음'}" for k, v in cfg.keys_status().items())]
        if self.ub.has_keys:
            try:
                krw, locked = self.ub.balance("KRW")
                out.append(f"✅ 업비트 연결 OK · 원화 {krw:,.0f}원 (주문에 묶임 {locked:,.0f}원)")
            except UpbitError as e:
                out.append(f"❌ 업비트 연결 실패: {e}" + (" → 허용 IP 확인" if e.ip_blocked else ""))
        try:
            uni = self.scanner.refresh_universe(force=True)
            out.append(f"감시 대상 {len(uni)}개: {', '.join(m[4:] for m in uni)}")
            btc = self.scanner.btc_context()
            out.append(f"BTC 상태 {btc.regime} · 24h {btc.ret24:+.2f}% · 7일 {btc.ret7d:+.2f}%")
            setups = self.scan_once(dry=True)
        except Exception as e:
            out.append(f"❌ 시세 조회 실패: {e}")
            setups = []
        for s in setups:
            out.append(f"  셋업 {s.market} {s.setup} 손절 -{s.stop_pct * 100:.1f}% · RS24 {s.rs24:+.1f} · "
                       f"거래량 {s.features['vol_ratio_1h']}배 · 4h {s.features['trend_4h']}")
        if not setups:
            out.append("  지금은 셋업 없음(정상 — 조건이 맞을 때만 잡혀요)")
        if (cfg.DEEPSEEK_API_KEY or cfg.ANTHROPIC_API_KEY) and setups:
            ver, text = llm.active_prompt()
            d = llm.judge(setups[0], ver, text)
            out.append(f"{'✅' if d['ok'] else '❌'} LLM 판단 테스트: {setups[0].market} → {d['action']}/{d['conviction']} "
                       f"{d['reason']}{' (실패: ' + d['error'] + ')' if d.get('error') else ''}")
        if notify.configured():
            ok, err = notify.send_test()
            out.append("✅ 텔레그램 OK" if ok else f"❌ 텔레그램 실패 {err}")
        return out
