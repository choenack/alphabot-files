# -*- coding: utf-8 -*-
"""
config.py — 모든 설정값 (로컬 단일 사용자용)
=====================================================
· 비밀키(업비트/DeepSeek/Claude/텔레그램)는 같은 폴더의 .env 에서만 읽는다. 코드에 절대 안 박음.
· 나머지 숫자는 '공격적' 기본값. .env 에 같은 이름으로 적으면 덮어쓴다.
· 운용 상한(MAX_CAPITAL_KRW): 계좌에 돈이 더 있어도 봇은 이 금액 + 봇이 번 수익까지만 쓴다.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:   # python-dotenv 미설치 방어
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")


def _s(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(_s(name, str(default))))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    return _s(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


class Config:
    # ── 비밀키 ─────────────────────────────────────────────
    UPBIT_ACCESS_KEY = _s("UPBIT_ACCESS_KEY")
    UPBIT_SECRET_KEY = _s("UPBIT_SECRET_KEY")
    DEEPSEEK_API_KEY = _s("DEEPSEEK_API_KEY")
    ANTHROPIC_API_KEY = _s("ANTHROPIC_API_KEY")
    TG_TOKEN = _s("TG_TOKEN")
    TG_CHAT = _s("TG_CHAT")

    # ── 경로 ───────────────────────────────────────────────
    DB_PATH = str(BASE_DIR / _s("DB_FILE", "coinai.db"))
    LOG_PATH = str(BASE_DIR / _s("LOG_FILE", "coinai.log"))

    # ── 운용 금액 ──────────────────────────────────────────
    MAX_CAPITAL_KRW = _f("MAX_CAPITAL_KRW", 100_000)     # 시험 운용 10만 원
    MIN_ORDER_KRW = _f("MIN_ORDER_KRW", 5_000)           # 업비트 최소 주문 금액
    FEE_RATE = _f("FEE_RATE", 0.0005)                    # 업비트 원화마켓 0.05%

    # ── 감시 대상 ──────────────────────────────────────────
    UNIVERSE_SIZE = _i("UNIVERSE_SIZE", 30)              # 24h 거래대금 상위 N개
    UNIVERSE_REFRESH_SEC = _i("UNIVERSE_REFRESH_SEC", 3600)
    MIN_LISTING_DAYS = _i("MIN_LISTING_DAYS", 7)         # 상장 7일 미만 제외
    STABLE_COINS = set(_s("STABLE_COINS", "USDT,USDC,USDS,USDE,DAI,FDUSD,PYUSD,TUSD,USD1").split(","))

    # ── 비중·손실 한도 (공격적) ─────────────────────────────
    MAX_POSITIONS = _i("MAX_POSITIONS", 3)
    MAX_POSITION_PCT = _f("MAX_POSITION_PCT", 0.45)      # 코인당 최대 비중
    RISK_HIGH = _f("RISK_HIGH", 0.03)                    # LLM 확신 high: 손절 시 운용금 3% 손실
    RISK_NORMAL = _f("RISK_NORMAL", 0.015)               # 확신 normal: 1.5%
    MAX_STOP_PCT = _f("MAX_STOP_PCT", 0.10)              # 손절 거리가 10% 넘는 셋업은 버림
    DAILY_LOSS_LIMIT = _f("DAILY_LOSS_LIMIT", 0.08)      # 하루 -8%면 그날 신규 진입 중지
    LOSS_STREAK_HALVE = _i("LOSS_STREAK_HALVE", 3)       # 연속 3패면 크기 절반(1승하면 복귀)
    TOTAL_STOP_DD = _f("TOTAL_STOP_DD", 0.25)            # 시작금 대비 -25%면 봇 정지
    REENTRY_COOLDOWN_SEC = _i("REENTRY_COOLDOWN_SEC", 2 * 3600)   # 같은 코인 청산 후 재진입 대기

    # ── BTC 상태별 신규 진입 배수 ───────────────────────────
    REGIME_MULT_UP = _f("REGIME_MULT_UP", 1.0)
    REGIME_MULT_SIDE = _f("REGIME_MULT_SIDE", 0.7)
    REGIME_MULT_DOWN = _f("REGIME_MULT_DOWN", 0.7)       # BTC 하락장: 'BTC와 상관없이 오르는 코인'만, 0.7배 크기
    DOWN_REGIME_MIN_RS = _f("DOWN_REGIME_MIN_RS", 3.0)   # 조건: 24h 상대강도 +3%p 이상
    DOWN_REGIME_MIN_RS7 = _f("DOWN_REGIME_MIN_RS7", 0.0)  #       그리고 7일 상대강도도 플러스
    KILL_BTC_DROP_1H = _f("KILL_BTC_DROP_1H", 0.03)      # BTC 1시간 고점 대비 -3% → 비상정지
    KILL_PAUSE_SEC = _i("KILL_PAUSE_SEC", 4 * 3600)

    # ── 셋업(코드 1차 필터) ─────────────────────────────────
    ATR_PERIOD = _i("ATR_PERIOD", 14)                    # 1시간봉 ATR
    BREAKOUT_LOOKBACK = _i("BREAKOUT_LOOKBACK", 20)      # 1시간봉 20개 고점 돌파
    BREAKOUT_VOL_MULT = _f("BREAKOUT_VOL_MULT", 2.0)     # 최근 1시간 거래량 ≥ 평소 2배
    BREAKOUT_MAX_EXT_ATR = _f("BREAKOUT_MAX_EXT_ATR", 1.5)  # 돌파선보다 1.5ATR 넘게 올라갔으면 추격 안 함
    BREAKOUT_STOP_ATR = _f("BREAKOUT_STOP_ATR", 1.0)     # 손절 = 돌파선 - 1ATR
    BREAKOUT_TIME_STOP_H = _f("BREAKOUT_TIME_STOP_H", 12)
    PULLBACK_RSI_MIN = _f("PULLBACK_RSI_MIN", 38)
    PULLBACK_RSI_MAX = _f("PULLBACK_RSI_MAX", 55)
    PULLBACK_NEAR_EMA_ATR = _f("PULLBACK_NEAR_EMA_ATR", 0.25)  # 1h 가격이 EMA20 + 0.25ATR 이하로 내려와야 눌림목
    PULLBACK_ENTRY_ATR = _f("PULLBACK_ENTRY_ATR", 0.5)   # 지정가 = 현재가 - 0.5ATR
    PULLBACK_EXPIRY_MIN = _i("PULLBACK_EXPIRY_MIN", 60)  # 1시간 안에 미체결이면 취소
    PULLBACK_STOP_ATR = _f("PULLBACK_STOP_ATR", 2.0)     # 손절 = 진입가 - 2ATR
    PULLBACK_TIME_STOP_H = _f("PULLBACK_TIME_STOP_H", 24)
    SETUP_RECHECK_SEC = _i("SETUP_RECHECK_SEC", 2 * 3600)  # 같은 셋업은 2시간 동안 LLM 재질문 안 함

    # ── 매도(코드) ─────────────────────────────────────────
    BE_TRIGGER_R = _f("BE_TRIGGER_R", 1.0)               # +1R 오면 손절선을 본전으로
    TRAIL_ATR = _f("TRAIL_ATR", 3.0)                     # 그 뒤 고점 대비 3ATR 빠지면 매도

    # ── 루프 주기 ──────────────────────────────────────────
    SCAN_EVERY_MIN = _i("SCAN_EVERY_MIN", 15)            # 15분봉 마감마다 스캔
    SCAN_DELAY_SEC = _i("SCAN_DELAY_SEC", 20)            # 봉 마감 후 20초 뒤(캔들 확정 대기)
    MONITOR_SEC = _f("MONITOR_SEC", 2.0)                 # 손절·트레일링 감시 주기
    ORDER_POLL_SEC = _f("ORDER_POLL_SEC", 3.0)

    # ── LLM ───────────────────────────────────────────────
    DEEPSEEK_BASE_URL = _s("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions")
    DEEPSEEK_MODEL = _s("DEEPSEEK_MODEL", "deepseek-flash")      # = DeepSeek-V4.1-Flash-1017
    DEEPSEEK_THINKING = _s("DEEPSEEK_THINKING", "disabled")      # disabled | enabled
    DEEPSEEK_EFFORT = _s("DEEPSEEK_EFFORT", "low")               # enabled일 때: low | high | max
    DEEPSEEK_PRICE_IN = _f("DEEPSEEK_PRICE_IN", 0.30)            # $/1M (피크 기준, 보수적)
    DEEPSEEK_PRICE_IN_CACHED = _f("DEEPSEEK_PRICE_IN_CACHED", 0.006)
    DEEPSEEK_PRICE_OUT = _f("DEEPSEEK_PRICE_OUT", 1.20)
    FALLBACK_MODEL = _s("FALLBACK_MODEL", "claude-sonnet-5-5")   # DeepSeek 실패 시에만
    FALLBACK_PRICE_IN = _f("FALLBACK_PRICE_IN", 2.0)
    FALLBACK_PRICE_OUT = _f("FALLBACK_PRICE_OUT", 10.0)
    EVOLVE_MODEL = _s("EVOLVE_MODEL", "claude-fable-5-1")        # 월 1회 자가진화
    EVOLVE_PRICE_IN = _f("EVOLVE_PRICE_IN", 10.0)
    EVOLVE_PRICE_OUT = _f("EVOLVE_PRICE_OUT", 50.0)
    LLM_TIMEOUT_SEC = _i("LLM_TIMEOUT_SEC", 60)
    LLM_PARALLEL = _i("LLM_PARALLEL", 8)
    FX_KRW_PER_USD = _f("FX_KRW_PER_USD", 1400)                  # 비용 보고용 환율

    # ── 자가진화 ───────────────────────────────────────────
    EVOLVE_ENABLED = _b("EVOLVE_ENABLED", True)
    EVOLVE_INTERVAL_DAYS = _i("EVOLVE_INTERVAL_DAYS", 30)
    EVOLVE_MIN_SAMPLES = _i("EVOLVE_MIN_SAMPLES", 60)            # 결과가 채워진 판단이 이만큼 있어야 진화
    SHADOW_DAYS = _i("SHADOW_DAYS", 14)                          # 새 프롬프트는 2주간 판단만(주문 X)
    SHADOW_MIN_PAIRS = _i("SHADOW_MIN_PAIRS", 30)
    SHADOW_MIN_EDGE = _f("SHADOW_MIN_EDGE", 0.05)                # 셋업당 기대 R이 0.05 이상 좋아야 교체

    # ── 기타 ───────────────────────────────────────────────
    UPBIT_BASE = _s("UPBIT_BASE", "https://api.upbit.com/v1")
    QUOTE_MAX_PER_SEC = _i("QUOTE_MAX_PER_SEC", 8)       # 공개 API 초당 10회 → 여유 8
    EXCHANGE_MAX_PER_SEC = _i("EXCHANGE_MAX_PER_SEC", 6)  # 주문 API 초당 8회 → 여유 6
    NOTIFY_ERRORS = _b("NOTIFY_ERRORS", True)

    def keys_status(self) -> dict:
        return {
            "upbit": bool(self.UPBIT_ACCESS_KEY and self.UPBIT_SECRET_KEY),
            "deepseek": bool(self.DEEPSEEK_API_KEY),
            "anthropic": bool(self.ANTHROPIC_API_KEY),
            "telegram": bool(self.TG_TOKEN and self.TG_CHAT),
        }


cfg = Config()
