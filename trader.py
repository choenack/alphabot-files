# -*- coding: utf-8 -*-
"""
trader.py — 주문 실행(한 줄로 차례대로) + 포지션 상태 관리
=====================================================
· 모든 주문은 이 스레드 하나가 순서대로 처리한다 → 잔고를 두 번 쓰는 일이 없다.
· 포지션 흐름: pending_entry → open → pending_exit → closed  (미체결 취소면 cancelled)
· 주문 전에 DB에 먼저 기록하고 identifier를 붙여 보낸다. 응답을 못 받아도 identifier로 다시 찾는다.
· 손절선 계산(update_stops)은 순수 함수 → 테스트로 검증.
"""
import logging
import queue
import threading
import time

import requests

import db
import messages
import notify
import risk
from config import cfg
from upbit import Upbit, UpbitError, fill_info

log = logging.getLogger("trader")


def update_stops(p: dict, price: float, now: float | None = None) -> tuple[dict, str | None]:
    """보유 포지션 1개의 손절선 갱신 + 청산 여부.
       반환: (DB에 쓸 변경값, 청산 사유 또는 None)
       · +1R 도달 → 손절선을 본전(수수료 포함)으로, 그 뒤 고점 - 3ATR 트레일링
       · 시간 손절: 정해진 시간 안에 +1R을 한 번도 못 가면 정리"""
    now = now or time.time()
    upd = {}
    entry, r = float(p["entry_price"]), float(p["r_value"] or 0)
    peak = max(float(p["peak_price"] or entry), price)
    if peak > float(p["peak_price"] or 0):
        upd["peak_price"] = peak
    stop = float(p["stop_price"])
    be = bool(p["be_moved"])
    if not be and r > 0 and peak >= entry + cfg.BE_TRIGGER_R * r:
        be = True
        upd["be_moved"] = 1
        stop = max(stop, entry * (1 + 2 * cfg.FEE_RATE + 0.001))
    if be:
        stop = max(stop, peak - cfg.TRAIL_ATR * float(p["atr_abs"]))
    if stop > float(p["stop_price"]):
        upd["stop_price"] = stop
    if price <= stop:
        return upd, ("trail" if be else "stop")
    if not be and p["opened_at"] and now - float(p["opened_at"]) >= float(p["time_stop_h"] or 24) * 3600:
        return upd, "time"
    return upd, None


class Trader:
    def __init__(self, ub: Upbit):
        self.ub = ub
        self.q: queue.Queue = queue.Queue()
        self.regime = "SIDE"           # 스캐너가 갱신
        self._last_poll = 0.0

    def submit(self, cmd: str, **kw) -> None:
        self.q.put((cmd, kw))

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                cmd, kw = self.q.get(timeout=1.0)
            except queue.Empty:
                cmd, kw = None, {}
            try:
                if cmd == "enter":
                    self._enter(**kw)
                elif cmd == "exit":
                    self._exit(**kw)
                elif cmd == "cancel_buys":
                    self._cancel_entries(kw.get("reason", ""))
                elif cmd == "cancel_entry":
                    self._cancel_entry(kw["pid"])
                if time.time() - self._last_poll >= cfg.ORDER_POLL_SEC:
                    self._last_poll = time.time()
                    self.poll()
            except UpbitError as e:
                self._upbit_error(e)
            except Exception as e:
                log.exception("주문 처리 오류")
                notify.error(f"주문 처리 오류: {e}", key="trader")

    def _upbit_error(self, e: UpbitError) -> None:
        if e.ip_blocked:
            notify.error("업비트가 이 PC의 IP를 거부했어요. 인터넷 IP가 바뀐 것 같아요 → 업비트 API 관리에서 "
                         "허용 IP를 새 주소로 바꿔 주세요. (그동안 주문·손절이 안 됩니다)", key="ip")
        else:
            notify.error(f"업비트 오류: {e}", key=f"upbit-{e.name}")

    # ── 진입 ────────────────────────────────────────────────────────
    def _enter(self, setup, decision: dict) -> None:
        m = setup.market
        if any(p["market"] == m for p in db.active_positions()):
            return
        held = [p["market"] for p in db.positions("open", "pending_exit")]
        prices = self.ub.prices(held) if held else {}
        krw_free, _ = self.ub.balance("KRW")
        size, why = risk.entry_size(setup, decision["conviction"], self.regime, prices, krw_free)
        if size <= 0:
            log.info("진입 안 함 %s %s: %s", m, setup.setup, why)
            return
        ident = self.ub.new_identifier("b")
        base = dict(market=m, setup=setup.setup, conviction=decision["conviction"], status="pending_entry",
                    entry_identifier=ident, signal_price=setup.price, stop_price=setup.stop_price,
                    init_stop=setup.stop_price, atr_abs=setup.atr_abs, time_stop_h=setup.time_stop_h,
                    decision_id=decision.get("decision_id"), note=decision.get("reason"))
        if setup.entry_type == "market":
            pid = db.insert_position(entry_type="market", budget_krw=size, **base)
            o = self._send(pid, lambda: self.ub.buy_market(m, size, ident), ident)
        else:
            px = float(self.ub.round_price(m, setup.limit_price))
            vol = size / px
            pid = db.insert_position(entry_type="limit", budget_krw=px * vol, limit_price=px,
                                     expires_at=time.time() + cfg.PULLBACK_EXPIRY_MIN * 60, **base)
            o = self._send(pid, lambda: self.ub.buy_limit(m, px, vol, ident), ident)
        if o:
            db.update_position(pid, entry_uuid=o["uuid"])
            if setup.entry_type == "limit":   # 시장가는 몇 초 안에 체결되니 '체결' 알림 하나만 보낸다
                p = db.position(pid)
                notify.send(messages.limit_wait_text(m, setup.setup, decision["conviction"], decision.get("reason", ""),
                                                     p["budget_krw"], p["limit_price"], setup.stop_pct))

    def _send(self, pid: int, fn, ident: str):
        """주문 전송. 응답을 못 받았으면 identifier로 실제 접수 여부를 확인한다.
           확인도 안 되면 pending_entry로 남겨 두고 poll()이 identifier로 계속 확인한다
           (접수됐는데 '취소'로 적어 버리면 장부에 없는 코인이 생기므로)."""
        try:
            return fn()
        except UpbitError as e:
            db.update_position(pid, status="cancelled", note=f"주문 거절: {e.name}", closed_at=time.time())
            self._upbit_error(e)
            return None
        except requests.exceptions.RequestException:
            time.sleep(2)
            try:
                return self.ub.order(identifier=ident)
            except Exception:
                return None

    # ── 청산 ────────────────────────────────────────────────────────
    def _exit(self, pid: int, reason: str) -> None:
        p = db.position(pid)
        if not p or p["status"] != "open":
            return
        m, cur = p["market"], p["market"].split("-", 1)[1]
        avail, _ = self.ub.balance(cur)
        qty = min(float(p["qty"]), avail)
        px = self.ub.prices([m]).get(m, p["entry_price"])
        if qty * px < cfg.MIN_ORDER_KRW:
            self._close(p, px, 0.0, qty, reason, note="최소 주문 금액 미만(먼지) — 계좌에 남김")
            return
        ident = self.ub.new_identifier("s")
        db.update_position(pid, status="pending_exit", exit_identifier=ident, exit_reason=reason)
        try:
            o = self.ub.sell_market(m, qty, ident)
        except requests.exceptions.RequestException:
            # 접수 여부를 모르면 pending_exit 유지 → poll()이 identifier로 확인(없으면 open으로 되돌림)
            return
        except UpbitError as e:
            db.update_position(pid, status="open")
            self._upbit_error(e)
            return
        db.update_position(pid, exit_uuid=o["uuid"])

    def _close(self, p: dict, exit_price: float, exit_fee: float, sold_qty: float, reason: str,
               note: str | None = None) -> None:
        qty = float(p["qty"])
        cost = qty * float(p["entry_price"]) + float(p["entry_fee"] or 0)
        proceeds = sold_qty * exit_price - exit_fee + (qty - sold_qty) * exit_price
        pnl = proceeds - cost
        r_krw = qty * float(p["r_value"] or 0)
        pnl_r = pnl / r_krw if r_krw > 0 else 0.0
        db.update_position(p["id"], status="closed", exit_price=exit_price, exit_fee=exit_fee,
                           exit_reason=reason, pnl_krw=pnl, pnl_r=pnl_r, closed_at=time.time(),
                           note=note or p["note"])
        risk.on_trade_closed(pnl)
        try:
            held = [x["market"] for x in db.positions("open", "pending_exit")]
            prices = self.ub.prices(held) if held else {}
        except Exception:
            prices = {}
        notify.send(messages.exit_text(p, exit_price, pnl, pnl_r, reason, prices))

    # ── 체결 확인 ───────────────────────────────────────────────────
    def poll(self) -> None:
        for p in db.positions("pending_entry"):
            self._poll_entry(p)
        for p in db.positions("pending_exit"):
            self._poll_exit(p)

    def _get_order(self, uuid, ident):
        if uuid:
            return self.ub.order(uuid=uuid)
        return self.ub.order(identifier=ident)

    def _poll_entry(self, p: dict) -> None:
        try:
            o = self._get_order(p["entry_uuid"], p["entry_identifier"])
        except UpbitError as e:
            if e.status == 404 or "not_found" in (e.name or ""):
                if time.time() - float(p["created_at"]) > 60:   # 방금 낸 주문은 조회가 늦을 수 있어 1분 기다림
                    db.update_position(p["id"], status="cancelled", note="주문 없음", closed_at=time.time())
                return
            raise
        if not p["entry_uuid"] and o.get("uuid"):
            db.update_position(p["id"], entry_uuid=o["uuid"])
        fi = fill_info(o)
        if fi["final"]:
            if fi["executed_volume"] > 0:
                self._activate(p, fi)
            else:
                db.update_position(p["id"], status="cancelled", closed_at=time.time(),
                                   note="미체결 취소" if p["entry_type"] == "limit" else "체결 없음")
                if p["entry_type"] == "limit":
                    log.info("지정가 미체결 취소 %s", p["market"])
                    notify.send(messages.limit_cancel_text(p))
            return
        exp = p["expires_at"]
        if p["entry_type"] == "limit" and exp and exp > 0 and time.time() > exp:
            self.ub.cancel(o["uuid"])
            db.update_position(p["id"], expires_at=-1)   # 취소 요청함(다음 조회에서 최종 처리)

    def _activate(self, p: dict, fi: dict) -> None:
        entry, qty = fi["avg_price"], fi["executed_volume"]
        stop = float(p["init_stop"])
        if stop >= entry * 0.995:   # 체결가가 예상보다 낮게 들어와 손절선이 붙어버린 경우 방어
            stop = entry * (1 - max(0.01, (float(p["signal_price"]) - stop) / float(p["signal_price"])))
        r_val = entry - stop
        db.update_position(p["id"], status="open", qty=qty, entry_price=entry, entry_fee=fi["paid_fee"],
                           stop_price=stop, init_stop=stop, r_value=r_val, peak_price=entry,
                           opened_at=time.time())
        if qty * entry < cfg.MIN_ORDER_KRW * 1.05:
            p2 = db.position(p["id"])
            self._close(p2, entry, 0.0, 0.0, "dust", note="일부만 체결돼 최소 주문 금액 미만 — 계좌에 남김")
            return
        notify.send(messages.fill_text(db.position(p["id"])))

    def _poll_exit(self, p: dict) -> None:
        try:
            o = self._get_order(p["exit_uuid"], p["exit_identifier"])
        except UpbitError as e:
            if e.status == 404 or "not_found" in (e.name or ""):
                db.update_position(p["id"], status="open")   # 매도 미접수 → 다시 감시
                return
            raise
        if not p["exit_uuid"] and o.get("uuid"):
            db.update_position(p["id"], exit_uuid=o["uuid"])
        fi = fill_info(o)
        if not fi["final"]:
            return
        if fi["executed_volume"] <= 0:
            db.update_position(p["id"], status="open")   # 매도 실패 → 다시 감시
            return
        self._close(p, fi["avg_price"], fi["paid_fee"], fi["executed_volume"], p["exit_reason"] or "stop")

    def _cancel_entry(self, pid: int) -> None:
        p = db.position(pid)
        if p and p["status"] == "pending_entry" and p["entry_uuid"] and (p["expires_at"] or 0) > 0:
            self.ub.cancel(p["entry_uuid"])
            db.update_position(pid, expires_at=-1)

    def _cancel_entries(self, reason: str) -> None:
        n = 0
        for p in db.positions("pending_entry"):
            if p["entry_type"] == "limit" and p["entry_uuid"] and (p["expires_at"] or 0) > 0:
                try:
                    self.ub.cancel(p["entry_uuid"])
                    db.update_position(p["id"], expires_at=-1)
                    n += 1
                except UpbitError as e:
                    log.warning("취소 실패 %s: %s", p["market"], e)
        if n:
            notify.send(f"⏸ 대기 중 매수 주문 {n}건 취소 ({reason})")

    # ── 시작 시 정리 ────────────────────────────────────────────────
    def reconcile(self) -> None:
        """PC를 다시 켰을 때: 대기 주문 상태를 거래소와 맞추고, 사라진 보유 코인을 정리."""
        self.poll()
        accounts = {a["currency"]: float(a.get("balance") or 0) + float(a.get("locked") or 0)
                    for a in self.ub.accounts()}
        for p in db.positions("open"):
            cur = p["market"].split("-", 1)[1]
            if accounts.get(cur, 0.0) < float(p["qty"]) * 0.98:
                px = self.ub.prices([p["market"]]).get(p["market"], p["entry_price"])
                self._close(p, px, 0.0, float(p["qty"]), "external", note="거래소에 수량이 없음(직접 매도 추정)")
