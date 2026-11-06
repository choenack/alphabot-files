# -*- coding: utf-8 -*-
"""
evolve.py — 사후 결과 채우기 + 월 1회 프롬프트 자가진화(Claude Fable 5.1) + 섀도 비교
=====================================================
1) backfill(): 24시간 지난 판단마다 '그 뒤 4h/24h 수익률, 24h 최고/최저'를 채운다.
2) 점수: ENTER 판단 = 24h 뒤 결과를 손절 거리로 나눈 R(손절선을 건드렸으면 -1, 최대 +3), SKIP = 0.
   → '셋업 1개당 기대 R'. 좋은 프롬프트일수록 높다.
3) 진화: 결과가 60개 이상 쌓이고 30일 지났으면 Fable 5.1이 기록을 분석해 새 프롬프트를 쓴다.
4) 섀도: 새 프롬프트는 바로 쓰지 않는다. 2주 동안 같은 셋업을 '판단만' 시키고(주문 X)
   기존 프롬프트와 같은 셋업끼리 점수를 비교 → 0.05R 이상 좋을 때만 교체.
CLI:  python evolve.py status | run | promote | reject | backfill
"""
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import db
import llm
import notify
from config import cfg

log = logging.getLogger("evolve")


# ── 사후 결과 ───────────────────────────────────────────────────────
def backfill(ub, limit: int = 200) -> int:
    n = 0
    for d in db.decisions_to_backfill(time.time())[:limit]:
        try:
            end = datetime.fromtimestamp(d["ts"] + 24 * 3600 + 3600, tz=timezone.utc)
            df = ub.candles(d["market"], "minutes/60", 27, to=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            start = datetime.fromtimestamp(d["ts"], tz=timezone.utc) - timedelta(hours=1)
            df = df[(df["ts"] >= start) & (df["ts"] < start + timedelta(hours=25))]
            if df.empty or not d["price"]:
                db.set_backfill(d["id"], None, None, None, None)
                continue
            p0 = float(d["price"])
            at4 = df[df["ts"] <= start + timedelta(hours=4)]
            f4 = (float(at4["close"].iloc[-1]) / p0 - 1) * 100 if len(at4) else None
            f24 = (float(df["close"].iloc[-1]) / p0 - 1) * 100
            fmax = (float(df["high"].max()) / p0 - 1) * 100
            fmin = (float(df["low"].min()) / p0 - 1) * 100
            db.set_backfill(d["id"], f4, f24, fmax, fmin)
            n += 1
        except Exception as e:
            log.warning("backfill 실패 %s: %s", d["market"], e)
    return n


def score(d: dict) -> float | None:
    """판단 1개의 점수(R). 결과가 없으면 None."""
    if d.get("fwd_24h") is None or not d.get("stop_pct"):
        return None
    if d["action"] != "ENTER":
        return 0.0
    sp = float(d["stop_pct"]) * 100   # % 단위
    if d["fwd_24h_min"] is not None and d["fwd_24h_min"] <= -sp:
        return -1.0
    return max(-1.0, min(3.0, float(d["fwd_24h"]) / sp))


def prompt_score(version: int, shadow: bool, since: float = 0) -> tuple[float, int]:
    rows = db.q("SELECT * FROM decisions WHERE prompt_version=? AND is_shadow=? AND backfilled=1 AND ts>=?",
                (version, 1 if shadow else 0, since))
    s = [x for x in (score(r) for r in rows) if x is not None]
    return (sum(s) / len(s) if s else 0.0), len(s)


# ── 진화 ────────────────────────────────────────────────────────────
_SYSTEM = """너는 코인 자동매매 봇의 '판단 프롬프트'를 개선하는 퀀트 리서처다.
봇 구조: 코드가 업비트 원화마켓에서 돌파(BREAKOUT)/눌림목(PULLBACK) 셋업을 찾고, 판단 모델(DeepSeek V4.1 Flash)이
프롬프트를 읽고 각 셋업에 ENTER/SKIP과 확신(high/normal)을 정한다. 가격·손절·익절·비중은 코드가 정한다.
확신 high는 위험 3%, normal은 1.5%로 진입한다. 사용자는 공격적으로 수익을 원한다.

과제: 아래 판단 기록과 사후 결과를 분석해서, 어떤 입력 조합에서 ENTER가 돈을 벌었고 잃었는지,
어떤 SKIP이 큰 기회를 놓쳤는지 찾아 '더 높은 셋업당 기대 R'을 낼 새 프롬프트를 써라.
규칙
- 기록에 실제로 보이는 패턴만 근거로 쓴다. 표본이 적은 패턴은 약하게 반영한다.
- 출력 형식 규칙(JSON, reason 목록)은 봇이 따로 붙이므로 프롬프트에 쓰지 마라.
- 입력 필드 설명은 유지하고, 판단 기준 부분을 고쳐라. 한국어로, 6000자 이내.
- changes에는 무엇을 왜 바꿨는지 3~6줄로 요약한다."""

_SCHEMA = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}, "changes": {"type": "string"}},
    "required": ["prompt", "changes"],
    "additionalProperties": False,
}


def _samples(active_version: int, limit: int = 300) -> list:
    rows = db.q("SELECT * FROM decisions WHERE prompt_version=? AND is_shadow=0 AND backfilled=1 "
                "AND fwd_24h IS NOT NULL ORDER BY ts DESC LIMIT ?", (active_version, limit))
    out = []
    for r in rows:
        f = json.loads(r["features"] or "{}")
        f.pop("market", None)
        f.pop("recent_trades", None)
        out.append({"in": f, "action": r["action"], "conv": r["conviction"], "reason": r["reason"],
                    "fwd_4h": round(r["fwd_4h"] or 0, 2), "fwd_24h": round(r["fwd_24h"], 2),
                    "max_24h": round(r["fwd_24h_max"] or 0, 2), "min_24h": round(r["fwd_24h_min"] or 0, 2),
                    "score_r": round(score(r) or 0, 2)})
    return out


def run(force: bool = False) -> str:
    if not cfg.ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY가 없어 진화를 건너뜀"
    if db.prompt_by_status("shadow") and not force:
        return "이미 비교 중인 새 프롬프트가 있음"
    ver, text = llm.active_prompt()
    samples = _samples(ver)
    if len(samples) < cfg.EVOLVE_MIN_SAMPLES and not force:
        return f"표본 부족({len(samples)}/{cfg.EVOLVE_MIN_SAMPLES})"
    trades = db.q("SELECT setup, conviction, pnl_r, exit_reason FROM positions WHERE status='closed' AND qty>0 "
                  "ORDER BY closed_at DESC LIMIT 100")
    avg, n = prompt_score(ver, False)
    user = (f"[현재 프롬프트 v{ver}]\n{text}\n\n"
            f"[현재 점수] 셋업당 기대 R {avg:.3f} (표본 {n})\n\n"
            f"[실제 매매 결과 최근 {len(trades)}건]\n{json.dumps(trades, ensure_ascii=False)}\n\n"
            f"[판단 기록 {len(samples)}건]\n{json.dumps(samples, ensure_ascii=False)}")

    import anthropic
    client = anthropic.Anthropic(timeout=900.0, max_retries=1)
    try:
        with client.beta.messages.stream(
            model=cfg.EVOLVE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": _SCHEMA}},
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIStatusError as e:
        return f"진화 실패: HTTP {e.status_code} {str(e.message)[:150]}"
    except anthropic.APIConnectionError:
        return "진화 실패: 연결 오류"
    u = msg.usage
    cost = (u.input_tokens * cfg.EVOLVE_PRICE_IN + u.output_tokens * cfg.EVOLVE_PRICE_OUT) / 1e6
    db.log_llm("evolve", msg.model, u.input_tokens, u.output_tokens, cost)
    db.put("last_evolve_ts", time.time())
    if msg.stop_reason == "refusal":
        return "진화 실패: 모델이 거절함"
    if msg.stop_reason == "max_tokens":
        return "진화 실패: 응답이 잘림"
    raw = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        out = json.loads(raw)
    except ValueError:
        return "진화 실패: 응답 JSON 파싱 오류"
    new_text = (out.get("prompt") or "").strip()
    if not (300 <= len(new_text) <= 8000):
        return f"진화 실패: 프롬프트 길이 이상({len(new_text)}자)"
    v = db.add_prompt(new_text, "shadow", out.get("changes", ""))
    notify.send(f"🧬 <b>새 판단 프롬프트 v{v}</b> — {cfg.SHADOW_DAYS}일간 비교만 합니다(주문 X)\n"
                f"{out.get('changes', '')[:600]}\n비용 약 {cost * cfg.FX_KRW_PER_USD:,.0f}원")
    return f"새 프롬프트 v{v} 생성(섀도 비교 시작)"


def maybe_run() -> str | None:
    if not cfg.EVOLVE_ENABLED or not cfg.ANTHROPIC_API_KEY or db.prompt_by_status("shadow"):
        return None
    last = float(db.get("last_evolve_ts", 0) or 0)
    if last == 0:
        db.put("last_evolve_ts", time.time())   # 첫 실행일부터 30일 뒤에 시작
        return None
    if time.time() - last < cfg.EVOLVE_INTERVAL_DAYS * 86400:
        return None
    return run()


# ── 섀도 비교 ───────────────────────────────────────────────────────
def compare_shadow() -> dict | None:
    sh = db.prompt_by_status("shadow")
    act = db.prompt_by_status("active")
    if not sh or not act:
        return None
    since = float(sh["shadow_started_at"] or sh["created_at"])
    shadow_rows = db.q("SELECT * FROM decisions WHERE prompt_version=? AND is_shadow=1 AND backfilled=1",
                       (sh["version"],))
    pairs = []
    for s in shadow_rows:
        a = db.q1("SELECT * FROM decisions WHERE is_shadow=0 AND market=? AND setup=? AND ABS(ts-?)<180 "
                  "AND backfilled=1 AND ts>=? ORDER BY ABS(ts-?) LIMIT 1",
                  (s["market"], s["setup"], s["ts"], since, s["ts"]))
        sa, ss = (score(a) if a else None), score(s)
        if sa is not None and ss is not None:
            pairs.append((sa, ss))
    n = len(pairs)
    return {"shadow": sh, "active": act, "pairs": n, "since": since,
            "score_active": sum(p[0] for p in pairs) / n if n else 0.0,
            "score_shadow": sum(p[1] for p in pairs) / n if n else 0.0}


def promote(c: dict) -> None:
    db.set_prompt_status(c["active"]["version"], "retired")
    db.set_prompt_status(c["shadow"]["version"], "active", score_active=c["score_active"],
                         score_shadow=c["score_shadow"])


def reject(c: dict) -> None:
    db.set_prompt_status(c["shadow"]["version"], "rejected", score_active=c["score_active"],
                         score_shadow=c["score_shadow"])


def maybe_conclude_shadow() -> str | None:
    c = compare_shadow()
    if not c:
        return None
    age_days = (time.time() - c["since"]) / 86400
    if age_days < cfg.SHADOW_DAYS:
        return None
    v = c["shadow"]["version"]
    if c["pairs"] < cfg.SHADOW_MIN_PAIRS:
        if age_days < cfg.SHADOW_DAYS * 2:
            return None   # 표본이 모일 때까지 최대 2배 기간 더 비교
        reject(c)
        msg = f"새 프롬프트 v{v} 폐기: 비교 표본 부족({c['pairs']}개)"
    elif c["score_shadow"] - c["score_active"] >= cfg.SHADOW_MIN_EDGE:
        promote(c)
        msg = (f"새 프롬프트 v{v} 채택: 셋업당 {c['score_active']:+.3f}R → {c['score_shadow']:+.3f}R "
               f"(비교 {c['pairs']}개)")
    else:
        reject(c)
        msg = (f"새 프롬프트 v{v} 폐기: {c['score_shadow']:+.3f}R vs 기존 {c['score_active']:+.3f}R "
               f"(비교 {c['pairs']}개)")
    notify.send("🧬 " + msg)
    return msg


def _status() -> None:
    ver, _ = llm.active_prompt()
    a, n = prompt_score(ver, False)
    print(f"활성 프롬프트 v{ver}: 셋업당 기대 R {a:+.3f} (결과 있는 판단 {n}개)")
    c = compare_shadow()
    if c:
        print(f"비교 중 v{c['shadow']['version']}: {(time.time() - c['since']) / 86400:.1f}일째, "
              f"비교 {c['pairs']}개, 기존 {c['score_active']:+.3f}R / 새것 {c['score_shadow']:+.3f}R")
    last = float(db.get("last_evolve_ts", 0) or 0)
    if last:
        print(f"마지막 진화: {datetime.fromtimestamp(last):%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        _status()
    elif cmd == "run":
        print(run(force="--force" in sys.argv))
    elif cmd == "backfill":
        from upbit import Upbit
        print(f"{backfill(Upbit())}개 채움")
    elif cmd in ("promote", "reject"):
        c = compare_shadow()
        if not c:
            print("비교 중인 새 프롬프트가 없습니다")
        else:
            (promote if cmd == "promote" else reject)(c)
            print(f"v{c['shadow']['version']} → {'채택' if cmd == 'promote' else '폐기'}")
    else:
        print(__doc__)
