# -*- coding: utf-8 -*-
"""
main.py — coin ai 실행 파일
=====================================================
python main.py              실전 매매 시작
python main.py --check      점검만(키·IP·잔고·감시 대상·셋업·LLM 1회) — 주문 안 함
python main.py --report 7   최근 7일 성과
python main.py --reset-halt 누적 손실로 정지된 봇을 다시 켬

스레드 4개
  · 스캔:   15분봉 마감마다 감시 대상 30개 셋업 검사 → 셋업만 LLM 판단 → ENTER면 주문 스레드로
  · 감시:   2초마다 보유 코인 가격 확인 → 손절·본전·트레일링·시간 손절, 1분마다 BTC 급락 확인
  · 주문:   진입·청산·체결 확인을 한 줄로 차례대로
  · 정비:   1시간마다 사후 결과 채우기, 자가진화·섀도 비교 일정 확인
"""
import argparse
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler

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

log = logging.getLogger("main")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", "%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cfg.LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def keep_awake(on: bool) -> None:
    """Windows: 봇이 도는 동안 PC가 절전 모드로 들어가지 않게 한다(화면은 꺼져도 됨)."""
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


class Bot:
    def __init__(self):
        self.ub = Upbit()
        self.scanner = Scanner(self.ub)
        self.trader = Trader(self.ub)
        self.stop = threading.Event()
        self._exit_sent: dict[int, float] = {}
        self._last_kill = 0.0
        self._last_snap = 0.0

    # ── 스캔 ────────────────────────────────────────────────────────
    def scan_once(self, dry: bool = False) -> list:
        if risk.halted():
            log.warning("봇 정지 상태 — 신규 진입 안 함 (python main.py --reset-halt 로 해제)")
            return []
        universe = self.scanner.refresh_universe()
        btc = self.scanner.btc_context()
        self.trader.regime = btc.regime
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
        if dry or not setups:
            return setups
        if time.time() < risk.paused_until() or len(active) >= cfg.MAX_POSITIONS:
            log.info("지금은 진입 불가(일시 정지 또는 보유 한도) — LLM 판단 생략")
            return setups
        ver, text = llm.active_prompt()
        for s in setups:
            self.scanner.mark_asked(s)
        with ThreadPoolExecutor(max_workers=cfg.LLM_PARALLEL) as ex:
            results = list(ex.map(lambda s: (s, llm.judge(s, ver, text)), setups))
        enters = [(s, d) for s, d in results if d["action"] == "ENTER"]
        for s, d in results:
            log.info("판단 %s %s → %s/%s %s%s", s.market, s.setup, d["action"], d["conviction"], d["reason"],
                     f" (실패: {d['error']})" if d.get("error") else "")
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
            self.stop.wait(max(5.0, nxt - now))

    # ── 감시 ────────────────────────────────────────────────────────
    def monitor_tick(self) -> None:
        held = db.positions("open", "pending_exit")
        markets = sorted({p["market"] for p in held} | {"KRW-BTC"})
        prices = self.ub.prices(markets)
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
        s = risk.snapshot(prices)
        db.snap_equity(s["equity"], s["cash"], s["invested"], prices.get("KRW-BTC"))
        day0 = risk.day_start_equity(s["equity"])
        if s["equity"] <= day0 * (1 - cfg.DAILY_LOSS_LIMIT) and not db.get("daily_limit_hit"):
            db.put("daily_limit_hit", True)
            notify.send(f"🛑 하루 손실 한도 도달({(s['equity'] / day0 - 1) * 100:.1f}%) — 오늘은 신규 진입 중지")
        if risk.check_total_stop(s["equity"]):
            self.trader.submit("cancel_buys", reason="봇 정지")
            notify.send(f"⛔ <b>봇 정지</b> 자산 {s['equity']:,.0f}원 (시작금 대비 -{cfg.TOTAL_STOP_DD * 100:.0f}% 도달)\n"
                        "신규 진입을 멈췄어요. 보유분 손절 감시는 계속합니다. 다시 켜려면 --reset-halt")
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

    # ── 시작/종료 ───────────────────────────────────────────────────
    def run(self) -> int:
        """반환 코드: 0 정상 종료, 2 설정 문제(run.bat이 재시작하지 않음). 예외로 죽으면 1 → run.bat이 재시작."""
        db.init()
        llm.active_prompt()
        if not self.ub.has_keys:
            print("업비트 API 키가 없어요. .env 파일에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 를 넣어 주세요.")
            return 2
        if not cfg.DEEPSEEK_API_KEY and not cfg.ANTHROPIC_API_KEY:
            print("LLM 키가 없어요. .env 파일에 DEEPSEEK_API_KEY 를 넣어 주세요.")
            return 2
        try:
            krw, _ = self.ub.balance("KRW")
        except UpbitError as e:
            print(f"업비트 연결 실패: {e}\n" + ("→ 업비트 API 관리에서 이 PC의 IP를 허용 IP로 등록하세요."
                                             if e.ip_blocked else "→ API 키와 권한(자산조회·주문하기)을 확인하세요."))
            return 2
        self.trader.reconcile()
        s = risk.snapshot({})
        notify.send(f"▶️ <b>시작</b> 운용금 {s['allocation']:,.0f}원 · 원화 잔고 {krw:,.0f}원\n"
                    f"보유/대기 {s['count']}개 · 판단 {cfg.DEEPSEEK_MODEL} · 감시 {cfg.UNIVERSE_SIZE}개")
        keep_awake(True)
        threads = [threading.Thread(target=self.trader.run, args=(self.stop,), name="trader", daemon=True),
                   threading.Thread(target=self.monitor_loop, name="monitor", daemon=True),
                   threading.Thread(target=self.scan_loop, name="scan", daemon=True),
                   threading.Thread(target=self.maintenance_loop, name="maint", daemon=True)]
        for t in threads:
            t.start()

        def _sig(*_):
            self.stop.set()
        signal.signal(signal.SIGINT, _sig)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _sig)
        try:
            while not self.stop.wait(1.0):
                pass
        finally:
            self.stop.set()
            for t in threads:
                t.join(timeout=10)
            try:
                self.trader._cancel_entries("봇 종료")   # 꺼져 있는 동안 체결되지 않게
            except Exception as e:
                log.warning("종료 시 대기 주문 취소 실패: %s", e)
            n = len(db.positions("open"))
            notify.send(f"⏹ 종료 — 보유 {n}개는 다시 켤 때까지 손절 감시가 멈춥니다" if n else "⏹ 종료")
            keep_awake(False)
        return 0

    def check(self) -> None:
        db.init()
        print("=== coin ai 점검 (주문 안 함) ===")
        print(f"운용 상한 {cfg.MAX_CAPITAL_KRW:,.0f}원 · 감시 {cfg.UNIVERSE_SIZE}개 · 동시 보유 {cfg.MAX_POSITIONS}개 · "
              f"위험 high {cfg.RISK_HIGH:.1%}/normal {cfg.RISK_NORMAL:.1%} · 하루 한도 -{cfg.DAILY_LOSS_LIMIT:.0%}")
        print("키:", {k: ("있음" if v else "없음") for k, v in cfg.keys_status().items()})
        if self.ub.has_keys:
            try:
                krw, locked = self.ub.balance("KRW")
                print(f"업비트 연결 OK · 원화 {krw:,.0f}원 (주문에 묶임 {locked:,.0f}원)")
            except UpbitError as e:
                print(f"업비트 연결 실패: {e}" + (" → 허용 IP 확인" if e.ip_blocked else ""))
        uni = self.scanner.refresh_universe(force=True)
        print(f"감시 대상 {len(uni)}개: {', '.join(m[4:] for m in uni)}")
        btc = self.scanner.btc_context()
        print(f"BTC 상태 {btc.regime} · 24h {btc.ret24:+.2f}% · 7일 {btc.ret7d:+.2f}%")
        setups = self.scan_once(dry=True)
        for s in setups:
            print(f"  셋업 {s.market} {s.setup} 손절 -{s.stop_pct * 100:.1f}% · RS24 {s.rs24:+.1f} · "
                  f"거래량 {s.features['vol_ratio_1h']}배 · 4h {s.features['trend_4h']}")
        if not setups:
            print("  지금은 셋업 없음(정상 — 조건이 맞을 때만 잡힙니다)")
        if cfg.DEEPSEEK_API_KEY and setups:
            ver, text = llm.active_prompt()
            d = llm.judge(setups[0], ver, text)
            print(f"LLM 판단 테스트: {setups[0].market} → {d['action']}/{d['conviction']} {d['reason']} "
                  f"{'(실패: ' + d['error'] + ')' if d.get('error') else ''}")
        if notify.configured():
            ok, err = notify.send_test()
            print("텔레그램:", "OK" if ok else f"실패 {err}")
        print("=== 점검 끝 ===")


def main() -> None:
    ap = argparse.ArgumentParser(description="coin ai — 업비트 코인 자동매매")
    ap.add_argument("--check", action="store_true", help="점검만 하고 주문은 안 함")
    ap.add_argument("--report", nargs="?", const="7", help="성과 보기(일수 또는 all)")
    ap.add_argument("--reset-halt", action="store_true", help="정지 해제")
    a = ap.parse_args()
    setup_logging()
    if a.report:
        db.init()
        print(report.text(None if a.report == "all" else float(a.report)).replace("<b>", "").replace("</b>", ""))
    elif a.reset_halt:
        db.init()
        risk.reset_halt()
        print(f"정지를 해제했어요. 새 기준금 {risk.allocation():,.0f}원에서 -{cfg.TOTAL_STOP_DD:.0%}가 되면 다시 멈춥니다.")
    elif a.check:
        Bot().check()
    else:
        sys.exit(Bot().run())


if __name__ == "__main__":
    main()
