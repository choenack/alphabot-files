# -*- coding: utf-8 -*-
"""
db.py — SQLite 저장소 (봇 상태가 PC를 껐다 켜도 이어지게)
=====================================================
테이블
  · kv        : 정지 여부, 일시정지 시각, 하루 시작 자산, 연속 손절 수, 실현 손익 등 상태값
  · positions : 봇이 연 포지션(진입 대기 → 보유 → 청산 대기 → 청산). 손절선·최고가를 계속 저장
  · decisions : LLM 판단 기록 + 나중에 채우는 사후 결과(4h/24h 수익률) — 자가진화 재료
  · prompts   : 판단 프롬프트 버전(active / shadow / retired / rejected)
  · equity    : 자산 스냅샷(최대 낙폭·BTC 비교용)
  · llm_calls : LLM 호출 비용 기록
[스레드] 연결 1개 + 락. 호출량이 작아서 이걸로 충분.
"""
import json
import sqlite3
import threading
import time

from config import cfg

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    setup TEXT NOT NULL,
    conviction TEXT,
    status TEXT NOT NULL,              -- pending_entry | open | pending_exit | closed | cancelled
    entry_type TEXT,                   -- market | limit
    entry_identifier TEXT,
    entry_uuid TEXT,
    limit_price REAL,
    signal_price REAL,
    budget_krw REAL,
    qty REAL DEFAULT 0,
    entry_price REAL,
    entry_fee REAL DEFAULT 0,
    stop_price REAL,
    init_stop REAL,
    r_value REAL,                      -- 진입가 - 초기손절 (원)
    atr_abs REAL,
    peak_price REAL,
    be_moved INTEGER DEFAULT 0,
    expires_at REAL,                   -- 지정가 대기 만료
    time_stop_h REAL,
    opened_at REAL,
    exit_identifier TEXT,
    exit_uuid TEXT,
    exit_reason TEXT,
    exit_price REAL,
    exit_fee REAL DEFAULT 0,
    pnl_krw REAL,
    pnl_r REAL,
    closed_at REAL,
    decision_id INTEGER,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market TEXT NOT NULL,
    setup TEXT NOT NULL,
    price REAL,
    stop_pct REAL,
    features TEXT,
    prompt_version INTEGER,
    is_shadow INTEGER DEFAULT 0,
    action TEXT,                       -- ENTER | SKIP
    conviction TEXT,
    reason TEXT,
    model TEXT,
    latency_ms INTEGER,
    cost_usd REAL DEFAULT 0,
    error TEXT,
    fwd_4h REAL, fwd_24h REAL, fwd_24h_max REAL, fwd_24h_min REAL,
    backfilled INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS prompts (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,              -- active | shadow | retired | rejected
    text TEXT NOT NULL,
    changes TEXT,
    shadow_started_at REAL,
    score_active REAL,
    score_shadow REAL
);

CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY,
    equity REAL, cash REAL, invested REAL, btc_price REAL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, purpose TEXT, model TEXT,
    in_tok INTEGER, out_tok INTEGER, cost_usd REAL
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(cfg.DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def init(path: str | None = None) -> None:
    """테스트에서 다른 DB 파일(또는 ':memory:')을 쓰고 싶을 때 path 지정."""
    global _conn
    with _lock:
        if path is not None:
            if _conn is not None:
                _conn.close()
                _conn = None
            cfg.DB_PATH = path
        conn()


def q(sql: str, args: tuple = ()) -> list:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args: tuple = ()) -> int:
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


# ── kv ──────────────────────────────────────────────────────────────
def get(key: str, default=None):
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def put(key: str, value) -> None:
    ex("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, json.dumps(value)))


# ── positions ───────────────────────────────────────────────────────
def insert_position(**f) -> int:
    f.setdefault("created_at", time.time())
    cols = ",".join(f.keys())
    marks = ",".join("?" for _ in f)
    return ex(f"INSERT INTO positions ({cols}) VALUES ({marks})", tuple(f.values()))


def update_position(pid: int, **f) -> None:
    if not f:
        return
    sets = ",".join(f"{k}=?" for k in f)
    ex(f"UPDATE positions SET {sets} WHERE id=?", tuple(f.values()) + (pid,))


def position(pid: int):
    return q1("SELECT * FROM positions WHERE id=?", (pid,))


def positions(*statuses: str) -> list:
    marks = ",".join("?" for _ in statuses)
    return q(f"SELECT * FROM positions WHERE status IN ({marks}) ORDER BY id", statuses)


def active_positions() -> list:
    """자금이 묶인 포지션(진입 대기 포함)."""
    return positions("pending_entry", "open", "pending_exit")


def closed_trades(since_ts: float = 0) -> list:
    return q("SELECT * FROM positions WHERE status='closed' AND qty>0 AND closed_at>=? ORDER BY closed_at",
             (since_ts,))


def last_exit_ts(market: str) -> float:
    r = q1("SELECT MAX(closed_at) AS t FROM positions WHERE market=? AND status IN ('closed','cancelled')",
           (market,))
    return float(r["t"]) if r and r["t"] else 0.0


# ── decisions ───────────────────────────────────────────────────────
def log_decision(**f) -> int:
    f.setdefault("ts", time.time())
    if isinstance(f.get("features"), (dict, list)):
        f["features"] = json.dumps(f["features"], ensure_ascii=False)
    cols = ",".join(f.keys())
    marks = ",".join("?" for _ in f)
    return ex(f"INSERT INTO decisions ({cols}) VALUES ({marks})", tuple(f.values()))


def decisions_to_backfill(now: float) -> list:
    return q("SELECT id, ts, market, price FROM decisions WHERE backfilled=0 AND ts<=? ORDER BY ts LIMIT 200",
             (now - 24 * 3600,))


def set_backfill(did: int, fwd_4h, fwd_24h, fwd_max, fwd_min) -> None:
    ex("UPDATE decisions SET fwd_4h=?, fwd_24h=?, fwd_24h_max=?, fwd_24h_min=?, backfilled=1 WHERE id=?",
       (fwd_4h, fwd_24h, fwd_max, fwd_min, did))


# ── prompts ─────────────────────────────────────────────────────────
def prompt_by_status(status: str):
    return q1("SELECT * FROM prompts WHERE status=? ORDER BY version DESC LIMIT 1", (status,))


def add_prompt(text: str, status: str, changes: str = "") -> int:
    return ex("INSERT INTO prompts(created_at, status, text, changes, shadow_started_at) VALUES(?,?,?,?,?)",
              (time.time(), status, text, changes, time.time() if status == "shadow" else None))


def set_prompt_status(version: int, status: str, **f) -> None:
    f["status"] = status
    sets = ",".join(f"{k}=?" for k in f)
    ex(f"UPDATE prompts SET {sets} WHERE version=?", tuple(f.values()) + (version,))


# ── equity / llm cost ───────────────────────────────────────────────
def snap_equity(equity: float, cash: float, invested: float, btc_price) -> None:
    ex("INSERT OR REPLACE INTO equity(ts, equity, cash, invested, btc_price) VALUES(?,?,?,?,?)",
       (time.time(), equity, cash, invested, btc_price))


def log_llm(purpose: str, model: str, in_tok: int, out_tok: int, cost_usd: float) -> None:
    ex("INSERT INTO llm_calls(ts, purpose, model, in_tok, out_tok, cost_usd) VALUES(?,?,?,?,?,?)",
       (time.time(), purpose, model, int(in_tok or 0), int(out_tok or 0), float(cost_usd or 0)))
