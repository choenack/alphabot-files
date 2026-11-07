# -*- coding: utf-8 -*-
"""텔레그램 알림 문구·명령 테스트 — 인터넷 없이 돌아간다."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import messages  # noqa: E402
import notify  # noqa: E402
from config import cfg  # noqa: E402
from upbit import MARKET_NAMES  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    db.init(":memory:")
    MARKET_NAMES["KRW-XRP"] = "리플"
    yield


def _open_pos(**kw):
    f = dict(market="KRW-XRP", setup="BREAKOUT", conviction="high", status="open", entry_type="market", qty=10.0,
             entry_price=3000.0, entry_fee=15.0, stop_price=2895.0, init_stop=2895.0, r_value=105.0, atr_abs=30.0,
             peak_price=3000.0, be_moved=0, opened_at=time.time() - 7200, time_stop_h=12, note="BREAKOUT_VOL+RS_STRONG")
    f.update(kw)
    return db.position(db.insert_position(**f))


def test_fill_text_has_name_amount_stop_and_reason():
    t = messages.fill_text(_open_pos())
    assert "리플(XRP)" in t and "30,000원" in t and "2,895원" in t and "−3.5%" in t
    assert "거래량 실린 돌파 · BTC보다 강함" in t and "확신 높음" in t


def test_status_and_exit_text():
    _open_pos()
    s = messages.status_text({"KRW-XRP": 3150.0}, 70000)
    assert "리플(XRP)" in s and "+5.00%" in s and "+1,485원" in s and "초기 손절" in s and "업비트 원화 70,000원" in s
    p = _open_pos(market="KRW-ETH")
    e = messages.exit_text(p, 3300.0, 2970.0, 2.83, "trail", {})
    assert "트레일링 익절" in e and "+2,970원" in e and "+2.83R" in e and "보유 2.0시간" in e


def test_today_text_lists_trades():
    p = _open_pos()
    db.update_position(p["id"], status="closed", pnl_krw=-1200.0, pnl_r=-1.0, exit_reason="stop", closed_at=time.time())
    t = messages.today_text({})
    assert "리플(XRP) −1,200원" in t and "1회 중 0승" in t


def test_telegram_commands_only_from_my_chat(monkeypatch):
    monkeypatch.setattr(cfg, "TG_CHAT", "111")
    sent = []
    monkeypatch.setattr(notify, "send", lambda t: sent.append(t))
    ups = [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/status"}},          # 남의 채팅 → 무시
           {"update_id": 6, "message": {"chat": {"id": 111}, "text": "안녕"}},             # 명령 아님 → 무시
           {"update_id": 7, "message": {"chat": {"id": 111}, "text": "/status@my_bot"}}]
    nxt = notify.handle_updates(ups, lambda text: "답:" + text)
    assert nxt == 8 and sent == ["답:/status@my_bot"]


def test_settings_restart_only_for_upbit_keys(tmp_path, monkeypatch):
    import config
    import main
    for k in ("TG_TOKEN", "TG_CHAT", "UPBIT_ACCESS_KEY"):
        monkeypatch.setenv(k, "")             # 테스트가 끝나면 원래대로 돌아오게
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    app = main.App(main.MemoryLog())
    app.bot = type("B", (), {"state": {"running": True}})()
    calls = []
    monkeypatch.setattr(app, "restart_engine", lambda: calls.append("restart") or (True, "실행 중"))
    r = app.apply_settings({"TG_TOKEN": "t", "TG_CHAT": "1"})
    assert calls == [] and "바로 적용" in r["msg"] and cfg.TG_CHAT == "1"
    app.apply_settings({"UPBIT_ACCESS_KEY": "a"})
    assert calls == ["restart"]
    monkeypatch.undo()
    cfg.reload()
