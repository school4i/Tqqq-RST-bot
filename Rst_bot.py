# -*- coding: utf-8 -*-
"""
RST-Trend v3.2  |  TQQQ 매매 지령 봇 (GitHub Actions 전용)
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

# --- 전략 파라미터 ---
RSI_BUY_THRESHOLD = float(os.environ.get("RSI_BUY_THRESHOLD", 35.0))   
FILTER_MIN_SCORE  = int(os.environ.get("FILTER_MIN_SCORE", 3))         
LOC_PRICE_BUFFER  = float(os.environ.get("LOC_PRICE_BUFFER", 1.03))    
SELL_RATIO        = float(os.environ.get("SELL_RATIO", 0.10))          
ZONE_ANCHOR_MODE  = os.environ.get("ZONE_ANCHOR_MODE", "PEAK")         
PEAK_WINDOW       = int(os.environ.get("PEAK_WINDOW", 252))            

# --- 구간 정의 ---
ZONE_TABLE = [
    (1,  -20.0, 0.20, 1, "구간 1 (0 ~ -20%)"),
    (2,  -40.0, 0.50, 2, "구간 2 (-20 ~ -40%)"),
    (3,  -60.0, 0.80, 4, "구간 3 (-40 ~ -60%)"),
    (4,  -75.0, 0.92, 6, "구간 4 (-60 ~ -75%)"),
    (5, -999.0, 1.00, 8, "구간 5 (-75% 이하)"),
]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")


# ============================================================
# 1. 상태 관리
# ============================================================
def load_state():
    defaults = {
        "initial_cash":      float(os.environ.get("INITIAL_CASH", 22555.0)),
        "cash":              float(os.environ.get("MY_CASH", 0.0)),
        "shares":            int(float(os.environ.get("MY_SHARES", 0))),
        "avg_price":         float(os.environ.get("MY_AVG_PRICE", 0.0)),
        "base_amount":       float(os.environ.get("BASE_AMOUNT", 0.0)),
        "entry_price":       float(os.environ.get("ENTRY_PRICE", 0.0)),   
        "tax_free_exhausted": os.environ.get("TAX_FREE_EXHAUSTED", "FALSE").upper() == "TRUE",
        "last_bar_date":     "",                                          
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            defaults.update({k: v for k, v in loaded.items() if k in defaults})
            print(f"[state] {STATE_FILE} 로드 완료")
        except Exception as e:
            print(f"[state] 파싱 실패({type(e).__name__}) -> 환경변수 폴백")
    return defaults


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[state] {STATE_FILE} 저장 완료")
    except Exception as e:
        print(f"[state] 저장 실패: {type(e).__name__}")


# ============================================================
# 2. 텔레그램
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
            if r.status_code == 400 and parse_mode:
                return send_telegram_message(message, parse_mode=None)
            r.raise_for_status()
            return True
        except Exception as e:
            if attempt < 2: time.sleep(2 ** attempt)
    return False


# ============================================================
# 3. 데이터 수집
# ============================================================
def fetch_ohlcv(ticker, retries=3):
    import yfinance as yf
    for i in range(retries):
        try:
            df = yf.Ticker(ticker).history(
                period="2y", interval="1d", auto_adjust=False, repair=True
            )
            if df is not None and not df.empty and len(df) >= 60:
                return df
        except Exception as e:
            if i < retries - 1: time.sleep(5 * (i + 1))
    return None

def prepare_indicators(df):
    df = df[df["Close"].notna()].copy()
    df["SMA5"]  = df["Close"].rolling(window=5).mean()
    df["SMA20"] = df["Close"].rolling(window=20).mean()

    delta = df["Close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    df["RSI"] = (100.0 - (100.0 / (1.0 + rs))).fillna(100.0)

    df["PEAK"] = df["Close"].rolling(PEAK_WINDOW, min_periods=20).max()
    return df

def validate_bar(df, state):
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    while len(df) > 0 and df.index[-1].date() >= today_et:
        df = df.iloc[:-1]

    if len(df) < 60:
        return None, None, "데이터 길이 부족(60봉 미만)"

    last_bar = df.index[-1].date()
    bar_age = (today_et - last_bar).days
    if bar_age > 4: 
        return None, None, f"데이터 지연: 최신봉 {last_bar} / 오늘(ET) {today_et}"

    is_duplicate = (str(last_bar) == state.get("last_bar_date", ""))
    return df, {"last_bar": last_bar, "today_et": today_et, "is_duplicate": is_duplicate}, None


def log_signal(row):
    try:
        df = pd.DataFrame([row])
        header = not os.path.exists(SIGNAL_LOG)
        df.to_csv(SIGNAL_LOG, mode="a", header=header, index=False, encoding="utf-8-sig")
    except Exception as e:
        pass


# ============================================================
# 4. 메인 전략
# ============================================================
def run_rst_strategy():
    state = load_state()

    INITIAL_CASH = float(state["initial_cash"])
    MY_CASH      = float(state["cash"])
    MY_SHARES    = int(state["shares"])
    MY_AVG_PRICE = float(state["avg_price"])
    BASE_AMOUNT  = float(state["base_amount"])
    ENTRY_PRICE  = float(state["entry_price"])
    TAX_FREE_EXHAUSTED = bool(state["tax_free_exhausted"])

    raw = fetch_ohlcv(TICKER)
    if raw is None:
        send_telegram_message(f"❌ *{TICKER} 데이터 조회 실패*. 오늘 지령을 생성하지 못했습니다.")
        raise RuntimeError("data fetch failed")

    df = prepare_indicators(raw)
    df, meta, err = validate_bar(df, state)
    if err:
        send_telegram_message(f"⚠️ *데이터 이상 감지*\n`{err}`\n오늘 지령은 보류합니다.")
        raise RuntimeError(err)

    if meta["is_duplicate"]:
        send_telegram_message(
            f"😴 *휴장 안내* | 미국 시장 직전 거래일({meta['last_bar']}) 데이터가 어제와 동일합니다.\n새로운 지령 없음 ➔ 【 대기 】"
        )
        return

    # 지표 스냅샷
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
    
    # 액면분할 방어 스위치
    drop_from_prev = ((price - prev_price) / prev_price) * 100
    if drop_from_prev < -33.0:
        msg = f"🚨 *[긴급 차단]* 전일 대비 주가가 {drop_from_prev:.1f}% 폭락했습니다.\n" \
              f"액면분할(Stock Split) 또는 비정상 데이터가 의심되어 봇 가동을 전면 중단합니다.\n" \
              f"확인 후 수동으로 state.json 평단/수량을 재조정하세요."
        send_telegram_message(msg)
        raise RuntimeError("Stock Split or Abnormal Price Drop Detected.")

    if ZONE_ANCHOR_MODE == "ENTRY" and ENTRY_PRICE > 0:
        anchor, anchor_label = ENTRY_PRICE, "최초진입가"
    else:
        anchor, anchor_label = peak, f"{PEAK_WINDOW}일 고점"
        
    zone_drop = ((price - anchor) / anchor) * 100 if anchor > 0 else 0.0
    pnl_rate = ((price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0.0

    spent_cash = INITIAL_CASH - MY_CASH
    spent_pct  = (spent_cash / INITIAL_CASH * 100) if INITIAL_CASH > 0 else 0.0 

    checks = {
        "20일선 아래":   price < sma20,
        "양봉 전환":     price > open_price,
        f"RSI<={RSI_BUY_THRESHOLD:.0f}": rsi <= RSI_BUY_THRESHOLD,
        "5일선 우상향":  sma5 > sma5_prev,
    }
    score = sum(1 for v in checks.values() if v)
    unmet = [k for k, v in checks.items() if not v]
    filter_pass = (score >= FILTER_MIN_SCORE) and checks["20일선 아래"]

    msg = [
        "🤖 *[RST-Trend v3.2]* 매매 가이드",
        f"🗓 기준봉: `{meta['last_bar']}` (미국 직전 거래일)",
        f"📊 {TICKER} 종가: `${price:,.2f}` | RSI: `{rsi:.1f}`",
        f"📈 5일선: `${sma5:,.2f}` (전일 `${sma5_prev:,.2f}`) | 20일선: `${sma20:,.2f}`",
        f"🎯 앵커({anchor_label}): `${anchor:,.2f}` | 앵커대비: `{zone_drop:+.1f}%`",
        f"🍏 평단: `${MY_AVG_PRICE:,.2f}` | 평가손익: `{pnl_rate:+.1f}%` | 보유 `{MY_SHARES:,}주`",
        f"💰 예수금: `${MY_CASH:,.2f}` | 누적소진율: `{spent_pct:.1f}%`",
        "─" * 15,
        "🔍 *[시장 추세 분석]*",
    ]

    msg.append("🔥 단기 정배열 (5일선 > 20일선)" if sma5 > sma20 else "❄️ 역배열 조정 국면 (5일선 <= 20일선)")
    msg.append(f"🧮 매수필터 점수: *{score}/4* (기준 {FILTER_MIN_SCORE}개 이상)"
               + (f" | 미충족: {', '.join(unmet)}" if unmet else " | 전 항목 충족 🚨"))
    msg.append("─" * 15)

    action, order_qty, order_cash = "HOLD", 0, 0.0

    # 무보유 상태 신규 진입 분기
    if MY_SHARES <= 0 or MY_AVG_PRICE <= 0:
        if filter_pass and MY_CASH > 0 and BASE_AMOUNT > 0:
            budget = min(BASE_AMOUNT, MY_CASH, INITIAL_CASH * ZONE_TABLE[0][2])
            qty = math.floor(budget / (price * LOC_PRICE_BUFFER))
            if qty > 0:
                action, order_qty, order_cash = "BUY_NEW", qty, qty * price
                msg.append(f"🌱 *[오늘 밤 주문]* 신규 진입 LOC 매수 ➔ 【 *{qty:,}주* 】 (예상 `${order_cash:,.0f}`)")
            else:
                msg.append("📢 *[오늘 밤 주문]* 신규 진입 신호이나 예수금 부족 ➔ 【 관망 】")
        else:
            msg.append("📢 *[오늘 밤 주문]* 무보유 상태, 진입 신호 대기 ➔ 【 관망 】 ⏱️")

    # 매수(물타기) 구간
    elif price < MY_AVG_PRICE:
        # 🌟 [버그 수정] 언패킹 오류 해결을 위해 인덱스로 안전하게 접근합니다.
        selected_zone = next((z for z in ZONE_TABLE if zone_drop > z[1]), ZONE_TABLE[-1])
        zone_num = selected_zone[0]
        ratio = selected_zone[2]
        multiplier = selected_zone[3]
        zone_name = selected_zone[4]
        
        max_allowed = INITIAL_CASH * ratio
        remaining_in_zone = max(0.0, max_allowed - spent_cash)

        msg.append(f"📍 현재 위치: *{zone_name}* [누적한도 {ratio*100:.0f}% / 잔여 `${remaining_in_zone:,.0f}`]")

        if remaining_in_zone <= 0:
            action = "CIRCUIT_BREAKER"
            msg.append("🛡️ *[오늘 밤 주문]* 구간 할당 현금 소진 ➔ 【 강제 서킷 브레이커 (매수 잠금) 】")
        elif not filter_pass:
            msg.append(f"📢 *[오늘 밤 주문]* 필터 {score}/4 로 기준 미달 ➔ 【 관망 】 ⏱️")
        elif MY_CASH <= 0:
            msg.append("📢 *[오늘 밤 주문]* 필터 충족하나 예수금 고갈 ➔ 【 관망 】")
        else:
            required = BASE_AMOUNT * multiplier
            budget   = min(required, remaining_in_zone, MY_CASH)
            qty      = math.floor(budget / (price * LOC_PRICE_BUFFER)) 
            if qty <= 0:
                msg.append("📢 *[오늘 밤 주문]* 필터 충족하나 한도/예수금 부족으로 0주 ➔ 【 관망 】")
            else:
                action, order_qty, order_cash = "BUY_ADD", qty, qty * price
                capped = " ⚠️구간한도로 축소" if budget < required - 1e-9 else ""
                msg.append(f"🛒 *[오늘 밤 주문]* LOC 매수 ➔ 🚨 【 *{qty:,}주* 】 ({multiplier}배 가속{capped}) | 예상 `${order_cash:,.0f}`")

    # 익절 구간
    else:
        is_dead_cross = (sma5_prev >= sma20_prev) and (sma5 < sma20)
        is_rsi_peak_out = (rsi_prev >= 70) and (rsi < rsi_prev) and (price < sma5) and (prev_price >= sma5_prev)

        if is_dead_cross or is_rsi_peak_out:
            reason = "RSI 과열 후 5일선 이탈" if is_rsi_peak_out else "데드크로스 발생"
            if TAX_FREE_EXHAUSTED:
                action = "HOLD_TAX"
                msg.append(f"🔔 *[오늘 밤 주문]* 매도 신호 감지({reason})\n🔒 그러나 올해 양도세 면세 소진 ➔ 【 홀딩 】\n⚠️ 단, 20일선까지 이탈 시 세금보다 손실이 큽니다. 수동 판단 권장.")
            else:
                qty = math.floor(MY_SHARES * SELL_RATIO)
                if qty > 0:
                    action, order_qty, order_cash = "SELL", qty, qty * price
                    msg.append(f"🚨 *[오늘 밤 주문]* 지정가 분할 익절 ➔ 💰 【 *{qty:,}주* 】 ({reason}) | 예상 `${order_cash:,.0f}`")
                else:
                    msg.append("📢 *[오늘 밤 주문]* 매도 수량 1주 미만 ➔ 【 보유 유지 】")
        else:
            msg.append("📢 *[오늘 밤 주문]* 추세 순항 중, 매도 없이 ➔ 【 즐겁게 홀딩 】 📈")

    # 가계부 자동 계산 출력
    if action in ("BUY_NEW", "BUY_ADD") and order_qty > 0:
        n_shares = MY_SHARES + order_qty
        n_cash   = MY_CASH - order_cash
        n_avg    = ((MY_AVG_PRICE * MY_SHARES) + order_cash) / n_shares
        msg += ["─" * 15, "📝 *가계부 (체결 성공 시 유저님이 state.json에 갱신할 값)*", f"`cash`: `{n_cash:,.2f}`", f"`shares`: `{n_shares:,}`", f"`avg_price`: `{n_avg:,.2f}`"]
    elif action == "SELL" and order_qty > 0:
        n_shares = MY_SHARES - order_qty
        n_cash   = MY_CASH + order_cash
        msg += ["─" * 15, "📝 *가계부 (체결 성공 시 유저님이 state.json에 갱신할 값)*", f"`cash`: `{n_cash:,.2f}`", f"`shares`: `{n_shares:,}`", "`avg_price`: 유지"]

    text = "\n".join(msg)
    send_telegram_message(text)
    
    state["last_bar_date"] = str(meta["last_bar"])
    save_state(state)
    log_signal({
        "bar_date": meta["last_bar"], "close": round(price, 2), "rsi": round(rsi, 2),
        "sma5": round(sma5, 2), "sma20": round(sma20, 2), "anchor": round(anchor, 2),
        "zone_drop": round(zone_drop, 2), "filter_score": score,
        "action": action, "qty": order_qty, "cash_used": round(order_cash, 2),
    })

if __name__ == "__main__":
    try:
        run_rst_strategy()
    except Exception as e:
        print(traceback.format_exc())
        send_telegram_message(f"🔥 *봇 실행 오류*\n`{type(e).__name__}: {str(e)[:300]}`")
        sys.exit(1)
