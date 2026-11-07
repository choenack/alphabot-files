# -*- coding: utf-8 -*-
"""
notify.py — 텔레그램 휴대폰 알림 + 명령 받기 (선택)
=====================================================
· TG_TOKEN / TG_CHAT 이 비어 있으면 조용히 아무 것도 안 한다.
· 보내기는 전용 스레드 하나가 줄 세워서 차례대로 → 매수·매도 알림 순서가 뒤바뀌지 않는다. 실패해도 매매에 영향 없음.
· 같은 에러 문구는 10분에 한 번만 보낸다(알림 폭탄 방지).
· 명령(/status 등)은 등록된 chat id에서 온 것만 처리한다. 다른 사람이 봇에 말을 걸어도 무시.
"""
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

from config import cfg

log = logging.getLogger("notify")
_TG_API = "https://api.telegram.org/bot{token}/{method}"
_recent: dict[str, float] = {}
_lock = threading.Lock()
_q: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None

COMMANDS = [("status", "지금 보유 코인·수익률·자산"), ("today", "오늘 매매와 손익"), ("report", "최근 7일 성과"),
            ("pause", "신규 진입 멈춤"), ("resume", "신규 진입 재개"), ("help", "명령 목록")]


def configured() -> bool:
    return bool(cfg.TG_TOKEN and cfg.TG_CHAT)


def _api(method: str, params: dict | None = None, timeout: float = 10) -> dict | None:
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(_TG_API.format(token=cfg.TG_TOKEN, method=method), data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        log.warning("텔레그램 %s 실패 [%s] %s", method, e.code, e.read().decode(errors="ignore")[:150])
    except Exception as e:
        log.warning("텔레그램 %s 실패: %s", method, e)
    return None


def _post(text: str) -> tuple[bool, str]:
    r = _api("sendMessage", {"chat_id": cfg.TG_CHAT, "text": text[:4000], "parse_mode": "HTML",
                             "disable_web_page_preview": True})
    return (True, "") if r and r.get("ok") else (False, "전송 실패(토큰·chat id 확인)")


def _send_loop() -> None:
    while True:
        text = _q.get()
        _post(text)
        time.sleep(0.4)   # 텔레그램 초당 한도 여유


def send(text: str) -> None:
    log.info("알림: %s", text.replace("\n", " | "))
    if not configured() or not text:
        return
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_send_loop, name="tg-send", daemon=True)
            _worker.start()
    _q.put(f"🤖 coin ai\n{text}")


def error(text: str, key: str | None = None) -> None:
    """에러 알림. key가 같으면 10분에 한 번만."""
    log.error(text)
    if not cfg.NOTIFY_ERRORS:
        return
    k = key or text[:80]
    with _lock:
        if time.time() - _recent.get(k, 0) < 600:
            return
        _recent[k] = time.time()
    send(f"⚠️ {text}")


def send_test() -> tuple[bool, str]:
    if not configured():
        return False, "TG_TOKEN 또는 TG_CHAT이 .env에 없습니다."
    return _post("✅ coin ai 알림 테스트 — 연결 정상")


# ── 명령 받기 ───────────────────────────────────────────────────────
def handle_updates(updates: list, handler) -> int | None:
    """getUpdates 결과 처리. 반환: 다음 offset. 내 chat id에서 온 '/명령'만 handler에 넘긴다."""
    nxt = None
    for u in updates:
        nxt = int(u["update_id"]) + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(cfg.TG_CHAT):
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        try:
            reply = handler(text)
        except Exception as e:
            log.exception("텔레그램 명령 처리 오류")
            reply = f"⚠️ 명령 처리 중 오류: {e}"
        if reply:
            send(reply)
    return nxt


def start_polling(handler, stop: threading.Event) -> None:
    """텔레그램 명령을 받는 스레드(롱폴링). 프로그램이 켜져 있는 동안 계속."""
    def loop():
        offset, ready = None, False
        while not stop.is_set():
            if not (configured() and cfg.TG_COMMANDS):
                ready = False
                stop.wait(30)
                continue
            if not ready:   # 꺼져 있던 동안 쌓인 옛 명령은 실행하지 않고 건너뛴다
                r = _api("getUpdates", {"offset": -1, "timeout": 0})
                if r is None:
                    stop.wait(30)
                    continue
                res = r.get("result") or []
                offset = int(res[-1]["update_id"]) + 1 if res else None
                _api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
                ready = True
            params = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            r = _api("getUpdates", params, timeout=35)
            if r is None:
                stop.wait(10)
                continue
            nxt = handle_updates(r.get("result") or [], handler)
            if nxt is not None:
                offset = nxt
    threading.Thread(target=loop, name="tg-poll", daemon=True).start()
