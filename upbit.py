# -*- coding: utf-8 -*-
"""
upbit.py — 업비트 REST 클라이언트 (공개 시세 + 내 계좌/주문)
=====================================================
[공개] 시세·캔들·마켓정보·호가단위. 초당 한도(10회)를 스스로 지켜 IP 차단을 피한다.
[개인] JWT(HS256) 인증. 주문은 identifier(내가 만든 고유번호)를 붙여 보낸다
       → 네트워크가 끊겨 응답을 못 받아도 identifier로 다시 조회해 '중복 주문'을 막는다.
[주의] 주문(POST)은 자동 재시도하지 않는다. 재시도는 반드시 identifier 조회로 확인한 뒤에만.
"""
import hashlib
import threading
import time
import uuid as _uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode, unquote

import jwt
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

from config import cfg


class UpbitError(Exception):
    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"[{status}] {name}: {message}")
        self.status, self.name, self.message = status, name, message

    @property
    def ip_blocked(self) -> bool:
        """허용 IP가 아닌 곳에서 주문 키를 쓴 경우(집 인터넷 IP가 바뀌면 생김)."""
        return "authorization_ip" in (self.name or "") or "IP" in (self.message or "")


class _RateLimiter:
    """직전 1초 호출 수가 한도를 넘으면 잠깐 쉰다."""
    def __init__(self, per_sec: int):
        self.max = max(1, int(per_sec))
        self._calls = deque()
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 1.0:
                self._calls.popleft()
            if len(self._calls) >= self.max:
                time.sleep(max(0.0, 1.0 - (now - self._calls[0]) + 0.02))
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 1.0:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


def _make_session(retry_get: bool) -> requests.Session:
    s = requests.Session()
    if retry_get and Retry is not None:
        r = Retry(total=3, connect=3, read=2, backoff_factor=0.4,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), raise_on_status=False)
        ad = HTTPAdapter(max_retries=r, pool_connections=10, pool_maxsize=20)
        s.mount("https://", ad)
    s.headers.update({"Accept": "application/json", "User-Agent": "coin-ai/1.0"})
    return s


# 호가단위 대체표(instruments API 실패 시에만). 실제 값은 /orderbook/instruments 가 우선.
#   일부러 '조금 큰' 단위로 잡았다 — 실제 단위의 배수라 주문이 거절되지 않는다.
_FALLBACK_TICKS = [
    (1_000_000, "1000"), (500_000, "500"), (100_000, "100"), (10_000, "10"),
    (1_000, "5"), (100, "1"), (10, "0.1"), (1, "0.01"), (0.1, "0.001"), (0, "0.0001"),
]

_UNIT_SEC = {"minutes/1": 60, "minutes/5": 300, "minutes/15": 900, "minutes/60": 3600,
             "minutes/240": 14400, "days": 86400}


def fmt_num(d: Decimal) -> str:
    """지수표기 없이 문자열로(업비트는 '1E+3' 같은 표기를 거부)."""
    s = format(d.normalize(), "f")
    return s if s not in ("-0", "") else "0"


class Upbit:
    def __init__(self, access: str = "", secret: str = "", base: str | None = None):
        self.access = access or cfg.UPBIT_ACCESS_KEY
        self.secret = secret or cfg.UPBIT_SECRET_KEY
        self.base = (base or cfg.UPBIT_BASE).rstrip("/")
        self._local = threading.local()   # 스레드마다 세션 따로(스캔·감시·주문 스레드가 동시에 씀)
        self._q_limit = _RateLimiter(cfg.QUOTE_MAX_PER_SEC)
        self._x_limit = _RateLimiter(cfg.EXCHANGE_MAX_PER_SEC)
        self._ticks: dict[str, str] = {}

    @property
    def _pub(self) -> requests.Session:
        if not hasattr(self._local, "pub"):
            self._local.pub = _make_session(retry_get=True)
        return self._local.pub

    @property
    def _priv(self) -> requests.Session:
        if not hasattr(self._local, "priv"):
            self._local.priv = _make_session(retry_get=False)
        return self._local.priv

    @property
    def has_keys(self) -> bool:
        return bool(self.access and self.secret)

    # ── 공통 ────────────────────────────────────────────────────────
    @staticmethod
    def _raise(r: requests.Response):
        try:
            e = r.json().get("error", {})
            raise UpbitError(r.status_code, e.get("name", "error"), e.get("message", r.text[:200]))
        except ValueError:
            raise UpbitError(r.status_code, "http_error", r.text[:200])

    def _public(self, path: str, params: dict | None = None):
        self._q_limit.wait()
        r = self._pub.get(f"{self.base}{path}", params=params, timeout=10)
        if r.status_code != 200:
            self._raise(r)
        return r.json()

    def _headers(self, params: dict | None) -> dict:
        payload = {"access_key": self.access, "nonce": str(_uuid.uuid4())}
        if params:
            qs = unquote(urlencode(params, doseq=True)).encode("utf-8")
            payload["query_hash"] = hashlib.sha512(qs).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    def _private(self, method: str, path: str, params: dict | None = None):
        if not self.has_keys:
            raise UpbitError(0, "no_keys", "업비트 API 키가 .env에 없습니다")
        self._x_limit.wait()
        url = f"{self.base}{path}"
        h = self._headers(params)
        if method == "GET":
            r = self._priv.get(url, params=params, headers=h, timeout=10)
        elif method == "POST":
            r = self._priv.post(url, json=params, headers=h, timeout=10)
        elif method == "DELETE":
            r = self._priv.delete(url, params=params, headers=h, timeout=10)
        else:
            raise ValueError(method)
        if r.status_code not in (200, 201):
            self._raise(r)
        return r.json()

    # ── 공개 시세 ───────────────────────────────────────────────────
    def markets(self) -> list:
        return self._public("/market/all", {"is_details": "true"})

    def tickers_all(self, quote: str = "KRW") -> list:
        return self._public("/ticker/all", {"quote_currencies": quote})

    def tickers(self, markets: list) -> dict:
        out = {}
        for i in range(0, len(markets), 100):
            chunk = markets[i:i + 100]
            for t in self._public("/ticker", {"markets": ",".join(chunk)}):
                out[t["market"]] = t
        return out

    def prices(self, markets: list) -> dict:
        return {m: float(t["trade_price"]) for m, t in self.tickers(markets).items()}

    def candles(self, market: str, unit: str, count: int = 200, to: str | None = None,
                closed_only: bool = True) -> pd.DataFrame:
        """오름차순 DataFrame(open/high/low/close/volume/value, ts=봉 시작 UTC).
           closed_only=True면 아직 안 끝난 마지막 봉을 뺀다."""
        params = {"market": market, "count": min(200, int(count))}
        if to:
            params["to"] = to
        rows = self._public(f"/candles/{unit}", params)
        if not rows:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "value"])
        df = pd.DataFrame([{
            "ts": datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc),
            "open": float(r["opening_price"]), "high": float(r["high_price"]),
            "low": float(r["low_price"]), "close": float(r["trade_price"]),
            "volume": float(r["candle_acc_trade_volume"]), "value": float(r["candle_acc_trade_price"]),
        } for r in rows]).sort_values("ts").reset_index(drop=True)
        if closed_only and len(df):
            end = df["ts"].iloc[-1] + timedelta(seconds=_UNIT_SEC.get(unit, 0))
            if end > datetime.now(timezone.utc):
                df = df.iloc[:-1].reset_index(drop=True)
        return df

    # ── 호가단위 ────────────────────────────────────────────────────
    def load_ticks(self, markets: list) -> None:
        need = [m for m in markets if m not in self._ticks]
        for i in range(0, len(need), 50):
            try:
                for r in self._public("/orderbook/instruments", {"markets": ",".join(need[i:i + 50])}):
                    self._ticks[r["market"]] = str(r["tick_size"])
            except Exception:
                pass

    def tick_size(self, market: str, price: float) -> Decimal:
        if market not in self._ticks:
            self.load_ticks([market])
        t = self._ticks.get(market)
        if t:
            return Decimal(t)
        for floor, tick in _FALLBACK_TICKS:
            if price >= floor:
                return Decimal(tick)
        return Decimal("0.00001")

    def round_price(self, market: str, price: float) -> str:
        """호가단위에 맞게 '내림'(매수 지정가가 의도보다 비싸지지 않게)."""
        tick = self.tick_size(market, price)
        d = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return fmt_num(d)

    @staticmethod
    def round_volume(vol: float) -> str:
        return fmt_num(Decimal(str(vol)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

    # ── 계좌·주문 ───────────────────────────────────────────────────
    def accounts(self) -> list:
        return self._private("GET", "/accounts")

    def balance(self, currency: str) -> tuple[float, float]:
        """(사용가능, 주문에 묶임)"""
        for a in self.accounts():
            if a.get("currency") == currency:
                return float(a.get("balance") or 0), float(a.get("locked") or 0)
        return 0.0, 0.0

    @staticmethod
    def new_identifier(prefix: str) -> str:
        return f"cai-{prefix}-{_uuid.uuid4().hex[:20]}"

    def buy_market(self, market: str, krw: float, identifier: str) -> dict:
        return self._private("POST", "/orders", {
            "market": market, "side": "bid", "ord_type": "price",
            "price": str(int(krw)), "identifier": identifier})

    def buy_limit(self, market: str, price: float, volume: float, identifier: str) -> dict:
        return self._private("POST", "/orders", {
            "market": market, "side": "bid", "ord_type": "limit",
            "price": self.round_price(market, price), "volume": self.round_volume(volume),
            "identifier": identifier})

    def sell_market(self, market: str, volume: float, identifier: str) -> dict:
        return self._private("POST", "/orders", {
            "market": market, "side": "ask", "ord_type": "market",
            "volume": self.round_volume(volume), "identifier": identifier})

    def order(self, uuid: str | None = None, identifier: str | None = None) -> dict:
        params = {"uuid": uuid} if uuid else {"identifier": identifier}
        return self._private("GET", "/order", params)

    def cancel(self, uuid: str) -> dict:
        return self._private("DELETE", "/order", {"uuid": uuid})


def fill_info(order: dict) -> dict:
    """주문 조회 결과 → 체결 수량/평균가/수수료/종결 여부.
       시장가 매수는 남은 금액이 환불되며 'cancel'로 끝나는 게 정상이라 executed_volume으로 판단."""
    trades = order.get("trades") or []
    vol = sum(float(t.get("volume") or 0) for t in trades)
    funds = sum(float(t.get("funds") or 0) for t in trades)
    exec_vol = float(order.get("executed_volume") or 0) or vol
    if vol > 0:
        avg = funds / vol
    else:
        avg = float(order.get("price") or 0)
    return {
        "executed_volume": exec_vol,
        "avg_price": avg,
        "funds": funds if funds else exec_vol * avg,
        "paid_fee": float(order.get("paid_fee") or 0),
        "remaining_volume": float(order.get("remaining_volume") or 0),
        "final": order.get("state") in ("done", "cancel"),
        "state": order.get("state"),
    }
