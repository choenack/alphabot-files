# -*- coding: utf-8 -*-
"""
llm.py — 셋업 판단(ENTER/SKIP + 확신 등급)
=====================================================
· 주력: DeepSeek V4.1 Flash (model='deepseek-flash'). JSON 출력 모드로 깨진 JSON 방지.
· 예비: DeepSeek이 실패(타임아웃·장애·빈 응답)할 때만 Claude Sonnet 5.5.
        구조화 출력(JSON 스키마)으로 형식 보장 + 서버측 fallbacks="default"(거절 시 대체 모델 자동 재시도).
· 둘 다 실패하면 SKIP(매매 안 함) — 실패가 손실로 이어지지 않게.
· 프롬프트 = 'DB의 활성 프롬프트(자가진화로 바뀜)' + '고정 출력 규칙'. 출력 규칙은 진화로 바뀌지 않는다.
"""
import json
import re
import time

import requests

import db
from config import cfg

REASON_VOCAB = [
    "BREAKOUT_VOL", "TREND_UP", "MOMENTUM", "RS_STRONG", "RS_WEAK", "BTC_UP", "BTC_WEAK",
    "PULLBACK_OK", "MACD_UP", "MACD_DOWN", "EXTENDED", "WEAK_VOLUME", "RSI_HOT",
    "FAKEOUT_RISK", "FALLING_KNIFE", "CAUTION_FLAG", "CHOPPY", "LOSS_HISTORY", "MIXED",
]
_VOCAB = set(REASON_VOCAB)
REASON_KO = {
    "BREAKOUT_VOL": "거래량 실린 돌파", "TREND_UP": "상승 추세", "MOMENTUM": "상승 힘이 강함",
    "RS_STRONG": "BTC보다 강함", "RS_WEAK": "BTC보다 약함", "BTC_UP": "BTC 상승", "BTC_WEAK": "BTC 약세",
    "PULLBACK_OK": "건강한 눌림", "MACD_UP": "MACD 상승", "MACD_DOWN": "MACD 하락", "EXTENDED": "이미 많이 오름",
    "WEAK_VOLUME": "거래량 약함", "RSI_HOT": "과열", "FAKEOUT_RISK": "가짜 돌파 위험",
    "FALLING_KNIFE": "떨어지는 칼날", "CAUTION_FLAG": "업비트 주의 표시", "CHOPPY": "방향 없음",
    "LOSS_HISTORY": "최근 손실 코인", "MIXED": "신호 혼재",
}


def reason_ko(code: str) -> str:
    return " · ".join(REASON_KO.get(t, t) for t in re.split(r"[+,/ ]+", code or "") if t) or "—"

DEFAULT_PROMPT = """너는 업비트 원화마켓 코인 '현물 매수 전용' 공격적 단기 추세 트레이더다.
코드가 이미 셋업(BREAKOUT=돌파 / PULLBACK=상승추세 눌림목)을 찾아 왔다. 너의 일은 이 셋업이 진짜인지 가짜인지 가려서
ENTER 또는 SKIP을 정하고, 진입한다면 확신 등급(high/normal)을 매기는 것이다. 가격·손절·익절은 코드가 정한다.
보유 기간은 몇 시간~며칠이고, 손절은 이미 정해져 있으며(stop_pct), 수익은 트레일링 스탑으로 크게 먹는 구조다.
승률보다 '크게 오를 자리'를 고르는 게 중요하다.

입력 필드:
- trend_4h: 4시간봉 추세(UP/SIDE/DOWN), btc_regime: BTC 상태(UP/SIDE/DOWN), btc_24h_pct: BTC 24시간 등락(%)
- bb_pos_1h: 1시간 볼린저 밴드 안 위치(0=하단, 100=상단, 100 초과=상단 돌파)
- dist_high20_pct / dist_low20_pct: 1시간봉 최근 20개 고점/저점 대비 현재가 위치(%)
- rsi_1h, rsi_dir, macd_1h: 1시간 모멘텀 상태
- vol_ratio_1h: 최근 1시간 거래량 / 평소 1시간 거래량
- atr_pct_1h: 1시간 변동폭(%), stop_pct: 손절까지 거리(%)
- mom_15m: 최근 1시간(15분봉 4개) 흐름
- rs_24h / rs_7d: BTC 대비 초과 수익률(%p). 플러스가 클수록 BTC보다 강하다
- caution: 업비트 주의 표시(PRICE_SWING, VOLUME_SOARING, DEPOSIT_SOARING, KIMCHI_GAP, SMALL_ACCOUNTS)
- recent_trades: 이 코인의 최근 매매 결과(R 단위, 음수=손실)

ENTER 쪽 근거: rs_24h와 rs_7d가 플러스, 돌파 시 거래량 배율이 높고 mom_15m이 UP 계열, trend_4h UP,
macd_1h가 CROSS_UP 또는 POS_RISING, btc_regime UP. 눌림목은 추세가 살아 있고 하락이 진정되는 모습.
SKIP 쪽 근거: 돌파인데 거래량이 기준을 겨우 넘고 rs가 마이너스(가짜 돌파), rsi_1h 80 이상인데 trend_4h가 UP이 아님(과열),
KIMCHI_GAP 또는 SMALL_ACCOUNTS 주의(작전성 펌핑), btc_regime DOWN인데 rs_24h가 약함,
눌림목인데 mom_15m STRONG_DOWN이고 macd_1h NEG_FALLING(떨어지는 칼날), 같은 코인 최근 연속 손실.
확신 high: ENTER 근거가 3개 이상 겹치고 SKIP 근거가 없을 때만. 조금이라도 애매하면 normal. 근거가 약하면 SKIP."""

OUTPUT_CONTRACT = (
    "\n\n[출력 규칙 - 반드시 지킬 것]\n"
    "JSON 객체 하나만 출력한다. 설명 문장, 코드블록, 다른 키는 금지.\n"
    '형식: {"action":"ENTER 또는 SKIP","conviction":"high 또는 normal","reason":"KEY+KEY"}\n'
    "reason은 다음 목록에서 최대 3개를 +로 연결: " + ", ".join(REASON_VOCAB) + "\n"
    '예시: {"action":"ENTER","conviction":"normal","reason":"BREAKOUT_VOL+RS_STRONG"}'
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ENTER", "SKIP"]},
        "conviction": {"type": "string", "enum": ["high", "normal"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "conviction", "reason"],
    "additionalProperties": False,
}

_claude = None


# ── 프롬프트 버전 ───────────────────────────────────────────────────
def active_prompt() -> tuple[int, str]:
    p = db.prompt_by_status("active")
    if not p:
        v = db.add_prompt(DEFAULT_PROMPT, "active", "기본 프롬프트")
        return v, DEFAULT_PROMPT
    return p["version"], p["text"]


def shadow_prompt():
    p = db.prompt_by_status("shadow")
    return (p["version"], p["text"]) if p else None


# ── 응답 정리 ───────────────────────────────────────────────────────
def parse_json(text: str):
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    t = re.sub(r"```(?:json)?", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", t)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def sanitize(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    action = str(d.get("action", "")).upper()
    if action not in ("ENTER", "SKIP"):
        return None
    conv = str(d.get("conviction", "normal")).lower()
    conv = conv if conv in ("high", "normal") else "normal"
    toks = [t.strip().upper() for t in re.split(r"[+,/ ]+", str(d.get("reason", ""))) if t.strip()]
    kept = [t for t in toks if t in _VOCAB][:3]
    return {"action": action, "conviction": conv, "reason": "+".join(kept) or "MIXED"}


# ── DeepSeek ────────────────────────────────────────────────────────
def _deepseek(system: str, user: str) -> tuple:
    """반환: (결과dict|None, 에러, 비용USD, 입력토큰, 출력토큰)"""
    if not cfg.DEEPSEEK_API_KEY:
        return None, "DEEPSEEK_API_KEY 없음", 0.0, 0, 0
    thinking = cfg.DEEPSEEK_THINKING == "enabled"
    body = {
        "model": cfg.DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "max_tokens": 4000 if thinking else 300,   # 생각 모드는 생각 토큰까지 포함되므로 넉넉히
        "stream": False,
    }
    if thinking:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = cfg.DEEPSEEK_EFFORT
    else:
        body["thinking"] = {"type": "disabled"}
        body["temperature"] = 0.2
    cost = 0.0
    in_tok = out_tok = 0
    err = ""
    for _ in range(2):   # JSON 모드는 가끔 빈 문자열을 준다 → 1회 재시도
        try:
            r = requests.post(cfg.DEEPSEEK_BASE_URL, json=body, timeout=cfg.LLM_TIMEOUT_SEC,
                              headers={"Authorization": f"Bearer {cfg.DEEPSEEK_API_KEY}",
                                       "Content-Type": "application/json"})
        except requests.exceptions.Timeout:
            err = "DeepSeek 타임아웃"
            continue
        except Exception as e:
            err = f"DeepSeek 연결 오류: {e}"
            continue
        if r.status_code != 200:
            err = f"DeepSeek HTTP {r.status_code}: {r.text[:150]}"
            if r.status_code in (400, 401, 402, 403):
                break   # 키·잔액 문제는 재시도해도 같음
            continue
        data = r.json()
        u = data.get("usage") or {}
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens") or max(0, int(u.get("prompt_tokens") or 0) - hit))
        o = int(u.get("completion_tokens") or 0)
        in_tok += hit + miss
        out_tok += o
        cost += (miss * cfg.DEEPSEEK_PRICE_IN + hit * cfg.DEEPSEEK_PRICE_IN_CACHED + o * cfg.DEEPSEEK_PRICE_OUT) / 1e6
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        res = sanitize(parse_json(content))
        if res:
            return res, "", cost, in_tok, out_tok
        err = "DeepSeek 응답 JSON 파싱 실패"
    return None, err, cost, in_tok, out_tok


# ── Claude 예비 ─────────────────────────────────────────────────────
def _claude_judge(system: str, user: str) -> tuple:
    if not cfg.ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY 없음", 0.0, 0, 0
    import anthropic
    global _claude
    if _claude is None:
        _claude = anthropic.Anthropic(timeout=float(cfg.LLM_TIMEOUT_SEC), max_retries=1)
    # Sonnet 5.5는 'disabled' 대신 'between_tools'가 가장 낮은 생각 설정(짧은 판단이라 생각 불필요)
    thinking = {"type": "between_tools"} if cfg.FALLBACK_MODEL.startswith("claude-sonnet-5-5") else {"type": "adaptive"}
    try:
        resp = _claude.beta.messages.create(
            model=cfg.FALLBACK_MODEL,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking=thinking,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError:
        return None, "Claude 요청 한도 초과", 0.0, 0, 0
    except anthropic.APIStatusError as e:
        return None, f"Claude HTTP {e.status_code}: {str(e.message)[:150]}", 0.0, 0, 0
    except anthropic.APIConnectionError:
        return None, "Claude 연결 오류", 0.0, 0, 0
    u = resp.usage
    in_tok, out_tok = int(u.input_tokens or 0), int(u.output_tokens or 0)
    cost = (in_tok * cfg.FALLBACK_PRICE_IN + out_tok * cfg.FALLBACK_PRICE_OUT) / 1e6
    if resp.stop_reason == "refusal":
        return None, "Claude 거절", cost, in_tok, out_tok
    text = next((b.text for b in resp.content if b.type == "text"), "")
    res = sanitize(parse_json(text))
    return (res, "" if res else "Claude 응답 파싱 실패", cost, in_tok, out_tok)


# ── 판단 ────────────────────────────────────────────────────────────
def judge(setup, prompt_version: int, prompt_text: str, shadow: bool = False) -> dict:
    """셋업 하나 판단 + DB 기록. 섀도 판단은 DeepSeek만 쓰고 주문하지 않는다."""
    system = prompt_text + OUTPUT_CONTRACT
    user = "다음 셋업을 판단하고 JSON으로만 답하라.\n" + json.dumps(setup.features, ensure_ascii=False)
    t0 = time.time()
    res, err, cost, i_t, o_t = _deepseek(system, user)
    model = cfg.DEEPSEEK_MODEL
    if res:
        db.log_llm("shadow" if shadow else "judge", model, i_t, o_t, cost)
    elif cost:
        db.log_llm("judge_failed", model, i_t, o_t, cost)
    if res is None and not shadow:
        res2, err2, cost2, i2, o2 = _claude_judge(system, user)
        if cost2:
            db.log_llm("judge_fallback", cfg.FALLBACK_MODEL, i2, o2, cost2)
        cost += cost2
        if res2:
            res, model = res2, cfg.FALLBACK_MODEL
        err = f"{err} / {err2}" if res2 is None else err
    out = res or {"action": "SKIP", "conviction": "normal", "reason": "MIXED"}
    did = db.log_decision(
        market=setup.market, setup=setup.setup, price=setup.price, stop_pct=setup.stop_pct,
        features=setup.features, prompt_version=prompt_version, is_shadow=1 if shadow else 0,
        action=out["action"], conviction=out["conviction"], reason=out["reason"], model=model,
        latency_ms=int((time.time() - t0) * 1000), cost_usd=round(cost, 6),
        error=None if res else (err or "판단 실패"))
    return dict(out, decision_id=did, ok=res is not None, error=err if res is None else "")


# ── 연결 테스트(대시보드 버튼) ───────────────────────────────────────
_TEST_FEATURES = {
    "market": "KRW-TEST", "setup": "BREAKOUT", "trend_4h": "UP", "btc_regime": "UP", "btc_24h_pct": 1.2,
    "bb_pos_1h": 104, "dist_high20_pct": 0.8, "dist_low20_pct": 9.5, "rsi_1h": 64.0, "rsi_dir": "UP",
    "macd_1h": "CROSS_UP", "vol_ratio_1h": 2.6, "atr_pct_1h": 1.9, "mom_15m": "UP", "ret_24h_pct": 6.1,
    "rs_24h": 4.9, "rs_7d": 7.3, "caution": [], "recent_trades": [], "stop_pct": 3.4,
}


def _friendly(err: str) -> str:
    """에러 문구 → 무엇을 하면 되는지."""
    e = err or ""
    if "없음" in e:
        return e + " — 설정 탭에서 입력하세요"
    if "401" in e or "authentication" in e.lower():
        return "키가 틀렸어요 — 키를 다시 복사해서 넣어 주세요"
    if "402" in e or "balance" in e.lower():
        return "잔액이 없어요 — 충전이 필요해요"
    if "403" in e:
        return "권한이 없어요 — 키 권한이나 계정 상태를 확인하세요"
    if "429" in e or "한도" in e:
        return "요청이 너무 많아요 — 잠시 뒤 다시 시도하세요"
    if "타임아웃" in e:
        return "응답이 없어요 — 인터넷 연결을 확인하거나 잠시 뒤 다시 시도하세요"
    if "연결" in e:
        return "서버에 연결할 수 없어요 — 인터넷·방화벽·백신 설정을 확인하세요"
    return e


def test_connection() -> list[str]:
    """예시 셋업 하나로 실제 판단을 요청해 본다. 판단 기록(자가진화 재료)에는 남기지 않고 비용만 기록."""
    system = active_prompt()[1] + OUTPUT_CONTRACT
    user = "다음 셋업을 판단하고 JSON으로만 답하라.\n" + json.dumps(_TEST_FEATURES, ensure_ascii=False)
    out = []
    for name, model, fn, enabled in (("DeepSeek", cfg.DEEPSEEK_MODEL, _deepseek, True),
                                     ("Claude 예비", cfg.FALLBACK_MODEL, _claude_judge, bool(cfg.ANTHROPIC_API_KEY))):
        if not enabled:
            out.append(f"– {name}: 키 없음(선택 사항 — DeepSeek 장애 때만 쓰여요)")
            continue
        t0 = time.time()
        res, err, cost, i_t, o_t = fn(system, user)
        ms = int((time.time() - t0) * 1000)
        if cost:
            db.log_llm("test", model, i_t, o_t, cost)
        won = cost * cfg.FX_KRW_PER_USD
        if res:
            out.append(f"✅ {name} 정상 ({model}) · 응답 {ms / 1000:.1f}초 · 비용 약 {won:.2f}원 · "
                       f"예시 판단 {res['action']}/{res['conviction']} {res['reason']}")
        else:
            out.append(f"❌ {name} 실패: {_friendly(err)}")
            if err and _friendly(err) != err:
                out.append(f"   (원래 메시지: {err[:160]})")
    return out
