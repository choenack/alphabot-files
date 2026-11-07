# -*- coding: utf-8 -*-
"""
main.py — coin ai 실행 파일 (exe의 시작점)
=====================================================
coin-ai.exe (또는 python main.py)   매매 엔진 + 대시보드 시작, 브라우저 자동 열림
  --check        점검만(키·IP·잔고·감시 대상·셋업·LLM 1회) — 주문 안 함
  --report 7     최근 7일 성과
  --reset-halt   누적 손실로 정지된 봇을 다시 켬
  --no-browser   브라우저 자동 열기 끄기

· 대시보드: http://127.0.0.1:8765  (상태·자산 곡선·보유 코인·매매 기록·판단 기록·설정·로그·버튼)
· 같은 프로그램이 두 번 켜지지 않는다(이미 켜져 있으면 대시보드만 열고 끝).
· 키가 없거나 IP가 막혀도 대시보드는 열린다 → 설정 탭에서 고치면 바로 엔진이 켜진다.
"""
import argparse
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from collections import deque
from logging.handlers import RotatingFileHandler

import db
import evolve
import llm
import messages
import notify
import report
import risk
from bot import Bot
from config import cfg, save_env
from dashboard import Dashboard
from upbit import Upbit

UPBIT_KEYS = ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY")

log = logging.getLogger("main")


class MemoryLog(logging.Handler):
    """대시보드 '로그' 탭에 보여줄 최근 로그."""
    def __init__(self, n: int = 400):
        super().__init__()
        self.lines: deque = deque(maxlen=n)

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


def setup_logging() -> MemoryLog:
    for stream in (sys.stdout, sys.stderr):   # 출력이 파일로 돌려져도 한글 때문에 죽지 않게
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", "%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cfg.LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    mem = MemoryLog()
    for h in (fh, sh, mem):
        h.setFormatter(fmt)
    root.handlers = [fh, sh, mem]
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return mem


def keep_awake(on: bool) -> None:
    """Windows: 프로그램이 도는 동안 PC가 절전 모드로 들어가지 않게 한다(화면은 꺼져도 됨)."""
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


class App:
    def __init__(self, memlog: MemoryLog):
        self.memlog = memlog
        self.exit = threading.Event()
        self.lock = threading.RLock()
        self.bot: Bot | None = None
        self.engine_msg = ""
        self.user_stopped = False
        self.evolving = False
        self.dash: Dashboard | None = None

    # ── 엔진 ────────────────────────────────────────────────────────
    def start_engine(self, user: bool = False, restarted: bool = False) -> tuple[bool, str]:
        with self.lock:
            if user:
                self.user_stopped = False
            if self.bot and self.bot.state["running"]:
                return True, "이미 실행 중"
            self.bot = Bot()
            ok, msg = self.bot.start(restarted=restarted)
            self.engine_msg = msg
            (log.info if ok else log.warning)("엔진: %s", msg)
            return ok, msg

    def stop_engine(self, reason: str, user: bool = False, restarting: bool = False) -> None:
        with self.lock:
            if user:
                self.user_stopped = True
            if self.bot:
                self.bot.shutdown(reason, restarting=restarting)
            self.engine_msg = reason

    def restart_engine(self) -> tuple[bool, str]:
        """설정 저장·스레드 복구용 재시작: 대기 주문은 그대로, '재시작 완료' 알림 하나만."""
        with self.lock:
            self.stop_engine("엔진 재시작", restarting=True)
            return self.start_engine(user=True, restarted=True)

    def apply_settings(self, updates: dict) -> dict:
        if not updates:
            return {"ok": True, "msg": "바뀐 값이 없어요"}
        save_env(updates)
        cfg.reload()
        msg = f"저장했어요({', '.join(sorted(updates))})"
        running = bool(self.bot and self.bot.state["running"])
        if running and any(k in updates for k in UPBIT_KEYS):
            ok, emsg = self.restart_engine()      # 업비트 키는 접속 객체가 들고 있어서 재시작 필요
            msg += f" · 엔진: {emsg}"
        elif not running:
            ok, emsg = self.start_engine(user=True)
            msg += f" · 엔진: {emsg}"
        else:
            msg += " · 바로 적용됐어요"          # 텔레그램·LLM 키·숫자 설정은 재시작 없이 반영
        return {"ok": True, "msg": msg}

    # ── 텔레그램 명령 ───────────────────────────────────────────────
    def _prices(self) -> tuple[dict, float | None]:
        b = self.bot
        if b and b.state["running"] and b.state.get("prices"):
            return b.state["prices"], b.state.get("krw_free")
        ub = b.ub if b else Upbit()
        messages.load_names(ub)
        try:
            return ub.prices(sorted({p["market"] for p in db.active_positions()} | {"KRW-BTC"})), None
        except Exception:
            return {}, None

    def tg_command(self, text: str) -> str:
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/status", "/상태", "/s"):
            prices, krw = self._prices()
            engine = "" if self.bot and self.bot.state["running"] else "\n⚠️ 매매 엔진이 꺼져 있어요(손절 감시 멈춤)"
            return messages.status_text(prices, krw) + engine
        if cmd in ("/today", "/오늘"):
            return messages.today_text(self._prices()[0])
        if cmd in ("/report", "/성과"):
            return report.text(7)
        if cmd in ("/pause", "/일시정지", "/멈춤"):
            db.put("entries_paused", True)
            if self.bot and self.bot.state["running"]:
                self.bot.trader.submit("cancel_buys", reason="텔레그램 일시정지")
            return "⏸ 신규 진입을 멈췄어요. 보유 코인의 손절·트레일링 감시는 계속해요. 다시 켜려면 /resume"
        if cmd in ("/resume", "/재개"):
            db.put("entries_paused", False)
            return "▶️ 신규 진입을 다시 켰어요."
        return messages.help_text()

    def run_check(self) -> list[str]:
        b = self.bot if self.bot else Bot()
        return b.check()

    def start_evolve(self) -> dict:
        if self.evolving:
            return {"ok": False, "error": "이미 진화 작업이 돌고 있어요"}
        if not cfg.ANTHROPIC_API_KEY:
            return {"ok": False, "error": "Claude API 키가 있어야 자가진화를 할 수 있어요"}

        def _job():
            self.evolving = True
            try:
                log.info("자가진화(수동): %s", evolve.run(force=True))
            except Exception as e:
                log.exception("자가진화 오류")
                notify.error(f"자가진화 오류: {e}", key="evolve")
            finally:
                self.evolving = False
        threading.Thread(target=_job, name="evolve", daemon=True).start()
        return {"ok": True, "msg": "자가진화를 시작했어요(몇 분 걸려요). 결과는 로그와 알림으로 와요"}

    def request_exit(self) -> None:
        threading.Timer(0.5, self.exit.set).start()   # 응답을 먼저 보내고 종료

    # ── 실행 ────────────────────────────────────────────────────────
    def run(self, open_browser: bool = True) -> int:
        db.init()
        llm.active_prompt()
        self.dash = Dashboard(self)
        try:
            self.dash.start()
        except OSError:
            url = f"http://127.0.0.1:{cfg.DASHBOARD_PORT}/"
            print(f"coin ai가 이미 켜져 있어요. 대시보드를 엽니다: {url}")
            if open_browser:
                webbrowser.open(url)
            return 2
        self.start_engine()
        notify.start_polling(self.tg_command, self.exit)
        if open_browser and cfg.OPEN_BROWSER:
            webbrowser.open(self.dash.url)
        print(f"\n  대시보드: {self.dash.url}   (이 창을 닫으면 봇이 꺼져요. 끌 때는 대시보드의 '종료' 버튼 권장)\n")
        keep_awake(True)

        def _sig(*_):
            self.exit.set()
        signal.signal(signal.SIGINT, _sig)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _sig)
        last_watch = last_retry = time.time()
        try:
            while not self.exit.wait(1.0):
                now = time.time()
                if now - last_watch >= 30:
                    last_watch = now
                    b = self.bot
                    if b and b.state["running"] and not b.alive():
                        log.error("엔진 스레드가 멈춰서 다시 켭니다")
                        notify.error("엔진 스레드가 멈춰서 자동으로 다시 켰어요", key="watchdog")
                        self.restart_engine()
                # 키는 있는데 연결 문제(IP·인터넷)로 못 켰으면 5분마다 다시 시도
                if (now - last_retry >= 300 and not self.user_stopped and not (self.bot and self.bot.state["running"])
                        and cfg.keys_status()["upbit"] and (cfg.DEEPSEEK_API_KEY or cfg.ANTHROPIC_API_KEY)):
                    last_retry = now
                    self.start_engine()
        finally:
            self.stop_engine("프로그램 종료")
            self.dash.shutdown()
            keep_awake(False)
        return 0


def selftest() -> int:
    """exe 빌드 확인용(개발자): 모듈·DB·대시보드가 묶여서 제대로 도는지 점검."""
    import json
    import urllib.request
    db.init(":memory:")
    app = App(setup_logging())
    d = Dashboard(app, host="127.0.0.1", port=0)
    d.start()
    app.dash = d
    html = urllib.request.urlopen(d.url, timeout=5).read().decode("utf-8")
    assert "coin ai" in html and d.token in html
    req = urllib.request.Request(d.url + "api/state", headers={"X-Token": d.token})
    st = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert st["status"] == "off" and "kpi" in st
    import anthropic   # exe에 SDK가 묶였는지
    assert hasattr(anthropic, "Anthropic")
    print("selftest: 대시보드·DB·모듈 OK")
    if os.environ.get("COINAI_SELFTEST_LLM") == "1":
        from strategy import Setup
        s = Setup("KRW-TEST", "BREAKOUT", "market", 100, None, 96, 0.04, 2, 12, 5.0, {"market": "KRW-TEST"})
        v, t = llm.active_prompt()
        print("selftest LLM:", llm.judge(s, v, t))
    d.shutdown()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="coin ai — 업비트 코인 자동매매")
    ap.add_argument("--check", action="store_true", help="점검만 하고 주문은 안 함")
    ap.add_argument("--report", nargs="?", const="7", help="성과 보기(일수 또는 all)")
    ap.add_argument("--reset-halt", action="store_true", help="정지 해제")
    ap.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 끄기")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    mem = setup_logging()
    if a.report:
        db.init()
        print(report.text(None if a.report == "all" else float(a.report)).replace("<b>", "").replace("</b>", ""))
    elif a.reset_halt:
        db.init()
        risk.reset_halt()
        print(f"정지를 해제했어요. 새 기준금 {risk.allocation():,.0f}원에서 -{cfg.TOTAL_STOP_DD:.0%}가 되면 다시 멈춥니다.")
    elif a.check:
        db.init()
        print("=== coin ai 점검 (주문 안 함) ===")
        for line in Bot().check():
            print(line)
        print("=== 점검 끝 ===")
    else:
        sys.exit(App(mem).run(open_browser=not a.no_browser))


if __name__ == "__main__":
    main()
