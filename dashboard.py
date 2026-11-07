# -*- coding: utf-8 -*-
"""
dashboard.py — 로컬 웹 대시보드 (http://127.0.0.1:8765)
=====================================================
· 표준 라이브러리 http.server만 사용(추가 설치 없음, exe에 그대로 묶임).
· 보안
  - 기본은 이 PC에서만 접속(127.0.0.1). Host 헤더도 확인해 다른 사이트가 우회 접속하는 걸 막는다.
  - 페이지에 심은 1회용 토큰이 없으면 API를 거부 → 다른 웹사이트가 몰래 버튼을 누를 수 없다.
  - 휴대폰(같은 와이파이)에서 보려면 DASHBOARD_HOST=0.0.0.0 + DASHBOARD_PASSWORD 필수.
  - 비밀키 값은 화면으로 절대 내보내지 않는다(있음/없음만).
"""
import base64
import hmac
import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import db
import evolve
import llm
import report
import risk
from config import RES_DIR, cfg

log = logging.getLogger("dashboard")

# 대시보드에서 바꿀 수 있는 설정(화이트리스트). 비밀키는 값이 비어 있으면 '변경 안 함'.
SECRET_KEYS = ["UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "TG_TOKEN"]
PLAIN_KEYS = {
    "TG_CHAT": str, "MAX_CAPITAL_KRW": float, "UNIVERSE_SIZE": int, "MAX_POSITIONS": int,
    "MAX_POSITION_PCT": float, "RISK_HIGH": float, "RISK_NORMAL": float, "DAILY_LOSS_LIMIT": float,
    "TOTAL_STOP_DD": float, "TRAIL_ATR": float, "DEEPSEEK_THINKING": str, "EVOLVE_ENABLED": int,
}


class Dashboard:
    def __init__(self, app, host: str | None = None, port: int | None = None):
        self.app = app
        self.token = secrets.token_urlsafe(24)
        host = host or cfg.DASHBOARD_HOST
        if host not in ("127.0.0.1", "localhost") and not cfg.DASHBOARD_PASSWORD:
            log.warning("DASHBOARD_PASSWORD 없이 외부 접속은 열 수 없어요 → 이 PC에서만 접속하도록 바꿉니다")
            host = "127.0.0.1"
        self.local_only = host in ("127.0.0.1", "localhost")
        self.host, self.port = host, cfg.DASHBOARD_PORT if port is None else port
        self.httpd = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        """포트가 이미 쓰이면 OSError → 프로그램이 이미 켜져 있다는 뜻(중복 실행 방지)."""
        dash = self

        class Handler(_Handler):
            pass
        Handler.dash = dash
        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True).start()
        log.info("대시보드: %s%s", self.url, "" if self.local_only else f" (같은 와이파이: http://<이 PC IP>:{self.port}/)")

    def shutdown(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # ── 데이터 ──────────────────────────────────────────────────────
    def state(self) -> dict:
        app = self.app
        bot = app.bot
        bst = dict(bot.state) if bot else {"running": False, "prices": {}}
        prices = bst.get("prices") or {}
        snap = risk.snapshot(prices)
        day0 = risk.day_start_equity(snap["equity"]) if db.get("day_key") else snap["equity"]
        now = time.time()
        if risk.halted():
            status, label = "halted", "정지됨(누적 손실 한도)"
        elif not bst.get("running"):
            status, label = "off", "엔진 꺼짐"
        elif risk.entries_paused():
            status, label = "paused", "신규 진입 일시정지"
        elif now < risk.paused_until():
            status, label = "paused", "BTC 급락으로 쉬는 중"
        else:
            status, label = "running", "실행 중"

        pos = []
        for p in db.active_positions():
            px = prices.get(p["market"])
            row = {k: p[k] for k in ("id", "market", "setup", "conviction", "status", "entry_type", "qty",
                                     "entry_price", "stop_price", "peak_price", "limit_price", "budget_krw",
                                     "be_moved", "opened_at", "created_at", "expires_at", "time_stop_h")}
            row["price"] = px
            if p["status"] in ("open", "pending_exit") and px and p["entry_price"]:
                row["pnl_pct"] = round((px / p["entry_price"] - 1) * 100, 2)
                row["pnl_krw"] = round(p["qty"] * (px - p["entry_price"]) - (p["entry_fee"] or 0))
                row["value"] = round(p["qty"] * px)
                row["stop_dist_pct"] = round((p["stop_price"] / px - 1) * 100, 2)
            pos.append(row)

        trades = db.q("SELECT id, market, setup, conviction, entry_price, exit_price, pnl_krw, pnl_r, exit_reason, "
                      "opened_at, closed_at, note FROM positions WHERE status='closed' AND qty>0 "
                      "ORDER BY closed_at DESC LIMIT 30")
        decisions = db.q("SELECT id, ts, market, setup, action, conviction, reason, model, latency_ms, cost_usd, error, "
                         "is_shadow, prompt_version, fwd_24h FROM decisions ORDER BY ts DESC LIMIT 40")
        s7, sall = report.stats(7), report.stats(None)
        month0 = time.mktime(time.localtime()[:2] + (1, 0, 0, 0, 0, 0, -1))
        cost_m = (db.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM llm_calls WHERE ts>=?", (month0,)) or {}).get("s", 0)
        ver, _ = llm.active_prompt()
        a_score, a_n = evolve.prompt_score(ver, False)
        sh = db.prompt_by_status("shadow")
        return {
            "now": now,
            "status": status, "status_label": label, "engine_msg": app.engine_msg,
            "started_at": bst.get("started_at"), "last_scan": bst.get("last_scan"), "next_scan": bst.get("next_scan"),
            "btc": bst.get("btc"), "universe": bst.get("universe", []), "setups": bst.get("setups", []),
            "krw_free": bst.get("krw_free"),
            "kpi": {
                "equity": round(snap["equity"]), "allocation": round(snap["allocation"]), "cash": round(snap["cash"]),
                "start_capital": cfg.MAX_CAPITAL_KRW, "today_pnl": round(snap["equity"] - day0),
                "today_pct": round((snap["equity"] / day0 - 1) * 100, 2) if day0 else 0,
                "realized": round(sall["pnl"]), "fees": round(sall["fees"]),
                "win_rate_7d": round(s7["win_rate"], 1), "trades_7d": s7["n"],
                "pf_7d": None if s7["pf"] in (0.0, float("inf")) else round(s7["pf"], 2),
                "pf_inf_7d": s7["pf"] == float("inf"),
                "avg_win_r": round(s7["avg_win_r"], 2), "avg_loss_r": round(s7["avg_loss_r"], 2),
                "mdd_all": round(sall["mdd"], 1), "trades_all": sall["n"], "win_rate_all": round(sall["win_rate"], 1),
                "llm_month_krw": round(float(cost_m or 0) * cfg.FX_KRW_PER_USD), "loss_streak": risk.loss_streak(),
            },
            "positions": pos, "trades": trades, "decisions": decisions,
            "prompt": {"version": ver, "score": round(a_score, 3), "samples": a_n,
                       "shadow": {"version": sh["version"], "since": sh["shadow_started_at"],
                                  "changes": sh["changes"]} if sh else None,
                       "last_evolve": db.get("last_evolve_ts", 0), "evolving": app.evolving},
            "settings": {
                "keys": cfg.keys_status(),
                "values": {k: int(v) if isinstance(v, bool) else v for k, v in ((k, getattr(cfg, k)) for k in PLAIN_KEYS)},
                "model": cfg.DEEPSEEK_MODEL, "fallback": cfg.FALLBACK_MODEL, "evolve_model": cfg.EVOLVE_MODEL,
                "lan": not self.local_only,
            },
            "flags": {"entries_paused": risk.entries_paused(), "halted": risk.halted(),
                      "pause_until": risk.paused_until(), "daily_limit_hit": bool(db.get("daily_limit_hit"))},
        }

    def equity(self, days: float | None) -> dict:
        since = time.time() - days * 86400 if days else 0
        rows = db.q("SELECT ts, equity, btc_price FROM equity WHERE ts>=? ORDER BY ts", (since,))
        if len(rows) > 400:   # 화면에 그릴 만큼만(시간 간격 고르게)
            step = len(rows) / 400
            rows = [rows[int(i * step)] for i in range(400)] + [rows[-1]]
        return {"points": [{"t": r["ts"], "eq": round(r["equity"]), "btc": r["btc_price"]} for r in rows]}

    # ── 동작 ────────────────────────────────────────────────────────
    def action(self, name: str, body: dict) -> dict:
        app = self.app
        if name == "pause":
            db.put("entries_paused", bool(body.get("paused")))
            if body.get("paused") and app.bot and app.bot.state["running"]:
                app.bot.trader.submit("cancel_buys", reason="대시보드 일시정지")
            return {"ok": True, "msg": "신규 진입을 멈췄어요(보유분 손절 감시는 계속)" if body.get("paused") else "신규 진입을 다시 켰어요"}
        if name == "reset_halt":
            risk.reset_halt()
            return {"ok": True, "msg": f"정지를 해제했어요. 새 기준금 {risk.allocation():,.0f}원"}
        if name in ("close", "cancel"):
            if not (app.bot and app.bot.state["running"]):
                return {"ok": False, "error": "엔진이 꺼져 있어요"}
            pid = int(body.get("id", 0))
            p = db.position(pid)
            if not p:
                return {"ok": False, "error": "포지션이 없어요"}
            if name == "close":
                app.bot.trader.submit("exit", pid=pid, reason="manual")
                return {"ok": True, "msg": f"{p['market']} 시장가 청산 주문을 넣었어요"}
            app.bot.trader.submit("cancel_entry", pid=pid)
            return {"ok": True, "msg": f"{p['market']} 대기 주문 취소를 요청했어요"}
        if name == "engine":
            act = body.get("action")
            if act == "start":
                ok, msg = app.start_engine(user=True)
            elif act == "stop":
                app.stop_engine("엔진 정지(대시보드)", user=True)
                ok, msg = True, "엔진을 껐어요(보유분 손절 감시도 멈춥니다)"
            else:
                ok, msg = app.restart_engine()
            return {"ok": ok, "msg" if ok else "error": msg}
        if name == "settings":
            return app.apply_settings(self._clean_settings(body))
        if name == "check":
            return {"ok": True, "lines": app.run_check()}
        if name == "llm_test":
            return {"ok": True, "lines": llm.test_connection()}
        if name == "evolve":
            return app.start_evolve()
        if name == "shutdown":
            app.request_exit()
            return {"ok": True, "msg": "프로그램을 종료해요"}
        return {"ok": False, "error": "알 수 없는 동작"}

    @staticmethod
    def _clean_settings(body: dict) -> dict:
        out = {}
        for k in SECRET_KEYS:
            v = str(body.get(k) or "").strip()
            if v:
                out[k] = v
        for k, typ in PLAIN_KEYS.items():
            if k not in body or body[k] in (None, ""):
                continue
            try:
                v = typ(body[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} 값이 올바르지 않아요")
            if k == "DEEPSEEK_THINKING" and v not in ("enabled", "disabled"):
                raise ValueError("DEEPSEEK_THINKING은 enabled 또는 disabled")
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            out[k] = v
        return out


class _Handler(BaseHTTPRequestHandler):
    dash: Dashboard = None
    server_version = "coin-ai"

    def log_message(self, fmt, *args):   # 접속 로그는 남기지 않는다(조용히)
        pass

    # ── 공통 ────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _allowed(self) -> bool:
        d = self.dash
        if d.local_only:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, b"forbidden", "text/plain")
                return False
        elif cfg.DASHBOARD_PASSWORD:
            auth = self.headers.get("Authorization") or ""
            ok = False
            if auth.startswith("Basic "):
                try:
                    _, pw = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
                    ok = hmac.compare_digest(pw, cfg.DASHBOARD_PASSWORD)
                except Exception:
                    ok = False
            if not ok:
                self._send(401, b"login required", "text/plain", {"WWW-Authenticate": 'Basic realm="coin ai"'})
                return False
        return True

    def _token_ok(self) -> bool:
        if hmac.compare_digest(self.headers.get("X-Token") or "", self.dash.token):
            return True
        self._json(403, {"ok": False, "error": "토큰이 맞지 않아요 — 페이지를 새로고침하세요"})
        return False

    # ── GET ─────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._allowed():
            return
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            html = (RES_DIR / "dashboard.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", self.dash.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                       {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                   "connect-src 'self'; frame-ancestors 'none'"})
            return
        if not u.path.startswith("/api/") or not self._token_ok():
            if not u.path.startswith("/api/"):
                self._send(404, b"not found", "text/plain")
            return
        try:
            if u.path == "/api/state":
                self._json(200, self.dash.state())
            elif u.path == "/api/equity":
                q = parse_qs(u.query)
                d = q.get("days", ["7"])[0]
                self._json(200, self.dash.equity(None if d == "all" else float(d)))
            elif u.path == "/api/logs":
                self._json(200, {"lines": list(self.dash.app.memlog.lines)[-300:]})
            else:
                self._json(404, {"ok": False, "error": "없는 주소"})
        except Exception as e:
            log.exception("대시보드 GET 오류")
            self._json(500, {"ok": False, "error": str(e)})

    # ── POST ────────────────────────────────────────────────────────
    def do_POST(self):
        if not self._allowed() or not self._token_ok():
            return
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 64_000)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._json(200, self.dash.action(u.path[len("/api/"):], body if isinstance(body, dict) else {}))
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            log.exception("대시보드 POST 오류")
            self._json(500, {"ok": False, "error": str(e)})
