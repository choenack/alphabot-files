# -*- coding: utf-8 -*-
"""
notify.py — 텔레그램 휴대폰 알림 (선택)
=====================================================
· TG_TOKEN / TG_CHAT 이 비어 있으면 조용히 아무 것도 안 한다.
· 전송은 백그라운드 스레드에서. 실패해도 매매에 영향 없음.
· 같은 에러 문구는 10분에 한 번만 보낸다(알림 폭탄 방지).
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from config import cfg

log = logging.getLogger("notify")
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"
_recent: dict[str, float] = {}
_lock = threading.Lock()


def configured() -> bool:
    return bool(cfg.TG_TOKEN and cfg.TG_CHAT)


def _post(text: str) -> tuple[bool, str]:
    body = json.dumps({"chat_id": cfg.TG_CHAT, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(_TG_API.format(token=cfg.TG_TOKEN), data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=8).read()
        return True, ""
    except urllib.error.HTTPError as e:
        return False, f"[{e.code}] {e.read().decode(errors='ignore')[:200]}"
    except Exception as e:
        return False, str(e)


def send(text: str) -> None:
    log.info("알림: %s", text.replace("\n", " | "))
    if not configured() or not text:
        return
    threading.Thread(target=_post, args=(f"🤖 coin ai\n{text}",), daemon=True).start()


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
