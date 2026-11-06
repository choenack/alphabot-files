# -*- coding: utf-8 -*-
"""대시보드 서버·설정 저장 테스트 — 인터넷 없이 돌아간다."""
import http.client
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
from collections import deque  # noqa: E402
from dashboard import Dashboard  # noqa: E402


class StubApp:
    def __init__(self):
        self.bot = None
        self.engine_msg = "테스트"
        self.evolving = False
        self.memlog = type("M", (), {"lines": deque(["로그 한 줄"])})()
        self.applied = None

    def apply_settings(self, updates):
        self.applied = updates
        return {"ok": True, "msg": "저장"}


@pytest.fixture()
def dash():
    db.init(":memory:")
    app = StubApp()
    d = Dashboard(app, host="127.0.0.1", port=0)
    d.start()
    yield d
    d.shutdown()


def _req(d, method, path, body=None, token=True, host=None):
    c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{d.port}"}
    if token:
        h["X-Token"] = d.token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    return r.status, r.read()


def test_page_has_token(dash):
    st, body = _req(dash, "GET", "/", token=False)
    assert st == 200 and dash.token.encode() in body and b"__TOKEN__" not in body


def test_api_requires_token_and_local_host(dash):
    assert _req(dash, "GET", "/api/state", token=False)[0] == 403
    assert _req(dash, "GET", "/", host="evil.example.com")[0] == 403      # DNS 리바인딩 차단
    st, body = _req(dash, "GET", "/api/state")
    s = json.loads(body)
    assert st == 200 and s["status"] == "off" and s["kpi"]["equity"] == config.cfg.MAX_CAPITAL_KRW
    assert "UPBIT_SECRET_KEY" not in body.decode()                       # 비밀키 값·이름이 새지 않음
    st, body = _req(dash, "GET", "/api/logs")
    assert json.loads(body)["lines"] == ["로그 한 줄"]


def test_pause_and_settings_actions(dash):
    st, body = _req(dash, "POST", "/api/pause", {"paused": True})
    assert st == 200 and json.loads(body)["ok"] and db.get("entries_paused") is True
    st, body = _req(dash, "POST", "/api/settings", {"MAX_CAPITAL_KRW": "150000", "UPBIT_ACCESS_KEY": " ", "DEEPSEEK_THINKING": "enabled"})
    assert st == 200 and dash.app.applied == {"MAX_CAPITAL_KRW": 150000, "DEEPSEEK_THINKING": "enabled"}
    st, body = _req(dash, "POST", "/api/settings", {"RISK_HIGH": "abc"})
    assert st == 400 and not json.loads(body)["ok"]
    assert _req(dash, "POST", "/api/pause", {"paused": False}, token=False)[0] == 403


def test_equity_endpoint(dash):
    db.snap_equity(100000, 100000, 0, 1e8)
    st, body = _req(dash, "GET", "/api/equity?days=all")
    pts = json.loads(body)["points"]
    assert st == 200 and pts[0]["eq"] == 100000


def test_lan_requires_password(monkeypatch):
    monkeypatch.setattr(config.cfg, "DASHBOARD_PASSWORD", "")
    d = Dashboard(StubApp(), host="0.0.0.0", port=0)
    assert d.local_only and d.host == "127.0.0.1"


def test_save_env_keeps_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# 주석\nUPBIT_ACCESS_KEY=old\n# MAX_POSITIONS=3\nOTHER=1\n", encoding="utf-8")
    config.save_env({"UPBIT_ACCESS_KEY": "new", "MAX_POSITIONS": 4, "TG_CHAT": "123"}, path=p)
    assert p.read_text(encoding="utf-8").splitlines() == [
        "# 주석", "UPBIT_ACCESS_KEY=new", "MAX_POSITIONS=4", "OTHER=1", "TG_CHAT=123"]
