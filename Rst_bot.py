
# -*- coding: utf-8 -*-
"""
RST-Trend v3  |  TQQQ 매매 지령 봇 (GitHub Actions 전용)
=========================================================
실행 시각 : KST 21:00 (= ET 07:00~08:00, 미국 정규장 개장 전)
동작      : 미국 직전 거래일 종가 기준으로 분석 → 텔레그램으로 '오늘 밤 주문' 지령 발송
주문 실행 : 사람이 직접 (봇은 지령만 발송, 자동 주문 없음)

v2 -> v3 변경 요약
  C1 구간 앵커를 '변동하는 평단' -> '52주 고점 / 최초진입가' 고정값으로 변경 (물타기 자기잠금 해소)
  C2 구간 잔여한도 클램프 추가 (한도 초과 매수 차단)
  C3 auto_adjust=False (배당조정가 vs 실제평단 좌표계 불일치 해소)
  C4 기준봉 날짜 검증 + 휴장일 중복 지령 차단
  C5 무보유(평단 0) 상태 신규 진입 분기 신설
  H1 데드크로스를 '상태' -> '전환 시점'으로 변경 (매일 10% 매도 방지)
  H2 상태값 state.json 파일 기반 관리 + 갱신값 자동 계산 안내
  H3 텔레그램 timeout / 재시도 / Markdown 400 폴백 / 토큰 마스킹
  H4 데이터 재시도 + 길이 가드 + 전역 예외 처리(실패 시 exit 1)
  H5 LOC 체결가 상향 버퍼 반영
  M1~M8 RSI 0나눗셈, 점수제 필터, ZeroDivision, 세금홀딩 완화, 0주 알림, 시그널 로깅 등
"""

import os
import sys
import json
import math
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

# ============================================================
# 0. 설정 (Configuration)
# ============================================================
TICKER            = os.environ.get("TICKER", "TQQQ")
STATE_FILE        = os.environ.get("STATE_FILE", "state.json")
SIGNAL_LOG        = os.environ.get("SIGNAL_LOG", "signals.csv")

# --- 전략 파라미터 (환경변수로 조정 가능) ---
RSI_BUY_THRESHOLD = float(os.environ.get("RSI_BUY_THRESHOLD", 35.0))   # M2: 30 -> 35 완화
FILTER_MIN_SCORE  = int(os.environ.get("FILTER_MIN_SCORE", 3))         # M2: 4중 필터 중 최소 충족 개수
LOC_PRICE_BUFFER  = float(os.environ.get("LOC_PRICE_BUFFER", 1.02))    # H5: LOC 체결가 +2% 버퍼
SELL_RATIO        = float(os.environ.get("SELL_RATIO", 0.10))          # 익절 시 보유 비중
ZONE_ANCHOR_MODE  = os.environ.get("ZONE_ANCHOR_MODE", "PEAK")         # C1: PEAK | ENTRY
PEAK_WINDOW       = int(os.environ.get("PEAK_WINDOW", 252))            # 52주

# --- 구간 정의 (앵커 대비 낙폭 하한, 누적 현금 허용 비율, 가속 승수) ---
# TQQQ는 3배 레버리지로 고점 대비 -80%도 실제 발생(2022년) -> 구간 4를 2단계로 세분화
ZONE_TABLE = [
    # (zone_num, 낙폭 하한(초과), 누적허용비율, 승수, 표기명)
    (1,  -20.0, 0.20, 1, "구간 1 (0 ~ -20%)"),
    (2,  -40.0, 0.50, 2, "구간 2 (-20 ~ -40%)"),
    (3,  -60.0, 0.80, 4, "구간 3 (-40 ~ -60%)"),
    (4,  -75.0, 0.92, 6, "구간 4 (-60 ~ -75%)"),
    (5, -999.0, 1.00, 8, "구간 5 (-75% 이하)"),
]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")


# ============================================================
# 1. 상태 관리 (H2)  -  state.json 우선, 없으면 환경변수 폴백
# ============================================================
def load_state():
    """state.json 을 최우선으로 읽고, 없으면 기존 GitHub Secrets(환경변수)로 폴백한다."""
    defaults = {
        "initial_cash":      float(os.environ.get("INITIAL_CASH", 22555.0)),
        "cash":              float(os.environ.get("MY_CASH", 0.0)),
        "shares":            int(float(os.environ.get("MY_SHARES", 0))),
        "avg_price":         float(os.environ.get("MY_AVG_PRICE", 0.0)),
        "base_amount":       float(os.environ.get("BASE_AMOUNT", 0.0)),
        "entry_price":       float(os.environ.get("ENTRY_PRICE", 0.0)),   # C1: 최초 진입가
        "tax_free_exhausted": os.environ.get("TAX_FREE_EXHAUSTED", "FALSE").upper() == "TRUE",
        "last_bar_date":     "",                                          # C4: 중복 지령 차단용
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            defaults.update({k: v for k, v in loaded.items() if k in defaults})
            print(f"[state] {STATE_FILE} 로드 완료")
        except Exception as e:
            print(f"[state] 파싱 실패({type(e).__name__}) -> 환경변수 폴백")
    else:
        print("[state] state.json 없음 -> 환경변수 사용")
    return defaults


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[state] {STATE_FILE} 저장 완료")
    except Exception as e:
        print(f"[state] 저장 실패: {type(e).__name__}")


# ============================================================
# 2. 텔레그램 (H3) - timeout / 재시도 / Markdown 400 폴백 / 토큰 마스킹
# ============================================================
def send_telegram_message(message, parse_mode="Markdown"):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[telegram] 설정 누락 - 콘솔 출력으로 대체\n" + message)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message[:4000], "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            # Markdown 파싱 실패(400)면 평문으로 1회 재시도 -> 지령 유실 방지
            if r.status_code == 400 and parse_mode:
                print("[telegram] Markdown 파싱 실패 -> 평문 재전송")
                return send_telegram_message(message, parse_mode=None)
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"[telegram] 전송 실패 ({attempt+1}/3): {type(e).__name__}")  # URL/토큰 미출력
            if attempt < 2:
                time.sleep(2 ** attempt)
    return False


# ============================================================
# 3. 데이터 수집 (C3, C4, H4)
# ============================================================
def fetch_ohlcv(ticker, retries=3):
    """auto_adjust=False -> Close 가 '실제 거래가'가 되어 평단/주문가와 좌표계 일치 (C3)"""
    import yfinance as yf
    for i in range(retries):
        try:
            df = yf.Ticker(ticker).history(
                period="2y", interval="1d", auto_adjust=False, repair=True
            )
            if df is not None and not df.empty and len(df) >= 60:
                return df
            print(f"[data] 데이터 부족 (len={0 if df is None else len(df)})")
        except Exception as e:
            print(f"[data] 조회 실패 ({i+1}/{retries}): {type(e).__name__}")
        if i < retries - 1:
            time.sleep(5 * (i + 1))
    return None


def prepare_indicators(df):
    """결측 제거 + 이평선 + Wilder RSI(14). M1: 0 나눗셈 안전 처리."""
    df = df[df["Close"].notna()].copy()

    df["SMA5"]  = df["Close"].rolling(window=5).mean()
    df["SMA20"] = df["Close"].rolling(window=20).mean()

    delta = df["Close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

    # M1: avg_loss == 0 -> inf/-inf 대신 NaN 경유 후 RSI 100 으로 명시
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    df["RSI"] = (100.0 - (100.0 / (1.0 + rs))).fillna(100.0)

    # C1-B: 앵커용 52주 고점
    df["PEAK"] = df["Close"].rolling(PEAK_WINDOW, min_periods=20).max()
    return df


def validate_bar(df, state):
    """C4: 미완성봉 제거 + 데이터 지연 감지 + 휴장일 중복 지령 차단"""
    today_et = datetime.now(ZoneInfo("America/New_York")).date()

    # KST 21시 = ET 07~08시(개장 전). 당일 날짜 봉이 있으면 미완성봉이므로 제거.
    while len(df) > 0 and df.index[-1].date() >= today_et:
        df = df.iloc[:-1]

    if len(df) < 60:
        return None, None, "데이터 길이 부족(60봉 미만)"

    last_bar = df.index[-1].date()
    bar_age = (today_et - last_bar).days
    if bar_age > 4:  # 주말 + 연휴 감안해도 4일 초과는 이상
        return None, None, f"데이터 지연: 최신봉 {last_bar} / 오늘(ET) {today_et}"

    is_duplicate = (str(last_bar) == state.get("last_bar_date", ""))
    return df, {"last_bar": last_bar, "today_et": today_et, "is_duplicate": is_duplicate}, None


# ============================================================
# 4. 구간 판정 (C1) - 앵커를 고정값으로
# ============================================================
def resolve_zone(zone_drop):
    for zone_num, lower, ratio, mult, name in ZONE_TABLE:
        if zone_drop > lower:
            return zone_num, ratio, mult, name
    z = ZONE_TABLE[-1]
    return z[0], z[2], z[3], z[4]


# ============================================================
# 5. 매수 필터 (M2) - 점수제
# ============================================================
def evaluate_filters(price, open_price, sma20, rsi, sma5, sma5_prev):
    checks = {
        "20일선 아래":   price < sma20,
        "양봉 전환":     price > open_price,
        f"RSI<={RSI_BUY_THRESHOLD:.0f}": rsi <= RSI_BUY_THRESHOLD,
        "5일선 우상향":  sma5 > sma5_prev,
    }
    score = sum(1 for v in checks.values() if v)
    unmet = [k for k, v in checks.items() if not v]
    return checks, score, unmet


# ============================================================
# 6. 시그널 로깅 (M7)
# ============================================================
def log_signal(row):
    try:
        df = pd.DataFrame([row])
        header = not os.path.exists(SIGNAL_LOG)
        df.to_csv(SIGNAL_LOG, mode="a", header=header, index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"[log] 기록 실패: {type(e).__name__}")


# ============================================================
# 7. 메인 전략
# ============================================================
def run_rst_strategy(df_override=None, state_override=None, dry_run=False):
    state = state_override if state_override is not None else load_state()

    INITIAL_CASH = float(state["initial_cash"])
    MY_CASH      = float(state["cash"])
    MY_SHARES    = int(state["shares"])
    MY_AVG_PRICE = float(state["avg_price"])
    BASE_AMOUNT  = float(state["base_amount"])
    ENTRY_PRICE  = float(state["entry_price"])
    TAX_FREE_EXHAUSTED = bool(state["tax_free_exhausted"])

    # ---------- 데이터 ----------
    raw = df_override if df_override is not None else fetch_ohlcv(TICKER)
    if raw is None:
        send_telegram_message(f"❌ *{TICKER} 데이터 조회 실패* (3회 재시도 소진). 오늘 지령을 생성하지 못했습니다.")
        raise RuntimeError("data fetch failed")   # H4: Actions 를 빨간불로

    df = prepare_indicators(raw)
    df, meta, err = validate_bar(df, state)
    if err:
        send_telegram_message(f"⚠️ *데이터 이상 감지*\n`{err}`\n오늘 지령은 보류합니다.")
        raise RuntimeError(err)

    if meta["is_duplicate"]:
        # C4: 미국 휴장일 -> 어제와 동일봉. 동일 매수 지령 반복 발송 차단.
        print(f"[skip] 기준봉 {meta['last_bar']} 은 이미 처리됨 (미국 휴장 추정)")
        if not dry_run:
            send_telegram_message(
                f"😴 *휴장 안내* | 미국 시장 직전 거래일({meta['last_bar']}) 데이터가 어제와 동일합니다.\n"
                "새로운 지령 없음 ➔ 【 대기 】"
            )
        return {"action": "SKIP_HOLIDAY"}

    # ---------- 지표 스냅샷 ----------
    price          = float(df["Close"].iloc[-1])
    open_price     = float(df["Open"].iloc[-1])
    prev_price     = float(df["Close"].iloc[-2])
    sma5           = float(df["SMA5"].iloc[-1])
    sma5_prev      = float(df["SMA5"].iloc[-2])
    sma20          = float(df["SMA20"].iloc[-1])
    sma20_prev     = float(df["SMA20"].iloc[-2])
    rsi            = float(df["RSI"].iloc[-1])
    rsi_prev       = float(df["RSI"].iloc[-2])
    peak           = float(df["PEAK"].iloc[-1])

    # ---------- C1: 구간 앵커 (고정값) ----------
    if ZONE_ANCHOR_MODE == "ENTRY" and ENTRY_PRICE > 0:
        anchor, anchor_label = ENTRY_PRICE, "최초진입가"
    else:
        anchor, anchor_label = peak, f"{PEAK_WINDOW}일 고점"
    zone_drop = ((price - anchor) / anchor) * 100 if anchor > 0 else 0.0

    # 평단 대비 손익은 '표시 전용' (구간 판정에 사용하지 않음)
    pnl_rate = ((price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0.0

    spent_cash = INITIAL_CASH - MY_CASH
    spent_pct  = (spent_cash / INITIAL_CASH * 100) if INITIAL_CASH > 0 else 0.0  # M4

    checks, score, unmet = evaluate_filters(price, open_price, sma20, rsi, sma5, sma5_prev)
    filter_pass = (score >= FILTER_MIN_SCORE) and checks["20일선 아래"]

    # ---------- 메시지 헤더 ----------
    msg = [
        "🤖 *[RST-Trend v3]* 매매 가이드",
        f"🗓 기준봉: `{meta['last_bar']}` (미국 직전 거래일)",
        f"📊 {TICKER} 종가: `${price:,.2f}` | RSI: `{rsi:.1f}`",
        f"📈 5일선: `${sma5:,.2f}` (전일 `${sma5_prev:,.2f}`) | 20일선: `${sma20:,.2f}`",
        f"🎯 앵커({anchor_label}): `${anchor:,.2f}` | 앵커대비: `{zone_drop:+.1f}%`",
        f"🍏 평단: `${MY_AVG_PRICE:,.2f}` | 평가손익: `{pnl_rate:+.1f}%` | 보유 `{MY_SHARES:,}주`",
        f"💰 예수금: `${MY_CASH:,.2f}` | 누적소진율: `{spent_pct:.1f}%`",
        "─" * 15,
        "🔍 *[시장 추세 분석]*",
    ]

    msg.append("🔥 단기 정배열 (5일선 > 20일선)" if sma5 > sma20
               else "❄️ 역배열 조정 국면 (5일선 <= 20일선)")
    msg.append(f"🧮 매수필터 점수: *{score}/4* (기준 {FILTER_MIN_SCORE}개 이상)"
               + (f" | 미충족: {', '.join(unmet)}" if unmet else " | 전 항목 충족 🚨"))
    msg.append("─" * 15)

    action, order_qty, order_cash = "HOLD", 0, 0.0

    # ================= C5: 무보유 -> 신규 진입 분기 =================
    if MY_SHARES <= 0 or MY_AVG_PRICE <= 0:
        if filter_pass and MY_CASH > 0 and BASE_AMOUNT > 0:
            budget = min(BASE_AMOUNT, MY_CASH, INITIAL_CASH * ZONE_TABLE[0][2])
            qty = math.floor(budget / (price * LOC_PRICE_BUFFER))
            if qty > 0:
                action, order_qty = "BUY_NEW", qty
                order_cash = qty * price
                msg.append(f"🌱 *[오늘 밤 주문]* 신규 진입 LOC 매수 ➔ 【 *{qty:,}주* 】"
                           f" (예상 `${order_cash:,.0f}`)")
            else:
                msg.append("📢 *[오늘 밤 주문]* 신규 진입 신호이나 예수금 부족 ➔ 【 관망 】")
        else:
            msg.append("📢 *[오늘 밤 주문]* 무보유 상태, 진입 신호 대기 ➔ 【 관망 】 ⏱️")

    # ================= 매수(물타기) 구간 =================
    elif price < MY_AVG_PRICE:
        zone_num, ratio, multiplier, zone_name = resolve_zone(zone_drop)
        max_allowed = INITIAL_CASH * ratio
        remaining_in_zone = max(0.0, max_allowed - spent_cash)   # C2

        msg.append(f"📍 현재 위치: *{zone_name}* [누적한도 {ratio*100:.0f}% / "
                   f"잔여 `${remaining_in_zone:,.0f}`]")

        if remaining_in_zone <= 0:
            action = "CIRCUIT_BREAKER"
            msg.append("🛡️ *[오늘 밤 주문]* 구간 할당 현금 소진 ➔ 【 강제 서킷 브레이커 (매수 잠금) 】")
        elif not filter_pass:
            msg.append(f"📢 *[오늘 밤 주문]* 필터 {score}/4 로 기준 미달 ➔ 【 관망 】 ⏱️")
        elif MY_CASH <= 0:
            msg.append("📢 *[오늘 밤 주문]* 필터 충족하나 예수금 고갈 ➔ 【 관망 】")
        else:
            # C2: 요구금액 / 구간잔여 / 실예수금 3중 클램프
            required = BASE_AMOUNT * multiplier
            budget   = min(required, remaining_in_zone, MY_CASH)
            qty      = math.floor(budget / (price * LOC_PRICE_BUFFER))   # H5 버퍼
            if qty <= 0:
                msg.append("📢 *[오늘 밤 주문]* 필터 충족하나 한도/예수금 부족으로 0주 ➔ 【 관망 】")  # M6
            else:
                action, order_qty = "BUY_ADD", qty
                order_cash = qty * price
                capped = " ⚠️구간한도로 축소" if budget < required - 1e-9 else ""
                msg.append(f"🛒 *[오늘 밤 주문]* LOC 매수 ➔ 🚨 【 *{qty:,}주* 】 "
                           f"({multiplier}배 가속{capped}) | 예상 `${order_cash:,.0f}`")

    # ================= 익절 구간 =================
    else:
        # H1: '상태'가 아닌 '전환 시점'만 포착 (매일 10% 매도 방지)
        is_dead_cross = (sma5_prev >= sma20_prev) and (sma5 < sma20)
        is_rsi_peak_out = (rsi_prev >= 70) and (rsi < rsi_prev) \
                          and (price < sma5) and (prev_price >= sma5_prev)

        if is_dead_cross or is_rsi_peak_out:
            reason = "RSI 과열 후 5일선 이탈" if is_rsi_peak_out else "데드크로스 발생"
            if TAX_FREE_EXHAUSTED:
                # M5: 무조건 홀딩 대신, 신호는 알리되 주문은 보류
                action = "HOLD_TAX"
                msg.append(f"🔔 *[오늘 밤 주문]* 매도 신호 감지({reason})")
                msg.append("🔒 그러나 올해 양도세 면세 소진 ➔ 【 홀딩 】")
                msg.append("⚠️ 단, 20일선까지 이탈 시 세금보다 손실이 큽니다. 수동 판단 권장.")
            else:
                qty = math.floor(MY_SHARES * SELL_RATIO)
                if qty > 0:
                    action, order_qty = "SELL", qty
                    order_cash = qty * price
                    msg.append(f"🚨 *[오늘 밤 주문]* 지정가 분할 익절 ➔ 💰 【 *{qty:,}주* 】 ({reason})"
                               f" | 예상 `${order_cash:,.0f}`")
                else:
                    msg.append("📢 *[오늘 밤 주문]* 매도 수량 1주 미만 ➔ 【 보유 유지 】")
        else:
            msg.append("📢 *[오늘 밤 주문]* 추세 순항 중, 매도 없이 ➔ 【 즐겁게 홀딩 】 📈")

    # ---------- H2: 체결 시 갱신할 상태값 자동 계산 ----------
    if action in ("BUY_NEW", "BUY_ADD") and order_qty > 0:
        n_shares = MY_SHARES + order_qty
        n_cash   = MY_CASH - order_cash
        n_avg    = ((MY_AVG_PRICE * MY_SHARES) + order_cash) / n_shares
        msg += ["─" * 15, "📝 *체결 시 갱신값 (state.json)*",
                f"`cash={n_cash:,.2f}` / `shares={n_shares:,}` / `avg_price={n_avg:,.2f}`"]
    elif action == "SELL" and order_qty > 0:
        n_shares = MY_SHARES - order_qty
        n_cash   = MY_CASH + order_cash
        msg += ["─" * 15, "📝 *체결 시 갱신값 (state.json)*",
                f"`cash={n_cash:,.2f}` / `shares={n_shares:,}` / `avg_price` 유지"]

    # ---------- 발송 & 기록 ----------
    text = "\n".join(msg)
    if not dry_run:
        send_telegram_message(text)
        state["last_bar_date"] = str(meta["last_bar"])   # C4
        save_state(state)
    log_signal({
        "bar_date": meta["last_bar"], "close": round(price, 2), "rsi": round(rsi, 2),
        "sma5": round(sma5, 2), "sma20": round(sma20, 2), "anchor": round(anchor, 2),
        "zone_drop": round(zone_drop, 2), "filter_score": score,
        "action": action, "qty": order_qty, "cash_used": round(order_cash, 2),
    })
    print(text)
    return {"action": action, "qty": order_qty, "cash": order_cash,
            "zone_drop": zone_drop, "score": score, "text": text}


# ============================================================
# 8. 엔트리포인트 (H4)
# ============================================================
if __name__ == "__main__":
    try:
        run_rst_strategy()
    except Exception as e:
        print(traceback.format_exc())
        send_telegram_message(f"🔥 *봇 실행 오류*\n`{type(e).__name__}: {str(e)[:300]}`")
        sys.exit(1)   # Actions 를 빨간불로 -> 즉시 인지 가능
