import os
import math
import requests
import yfinance as yf
import pandas as pd

# ====================================================
# ⚙️ [완벽 보안] 깃허브 시크릿 금고에서 값을 읽어옵니다.
# ====================================================
MY_SHARES = int(os.environ.get("MY_SHARES", 0))          
MY_CASH = float(os.environ.get("MY_CASH", 0.0))          
MY_AVG_PRICE = float(os.environ.get("MY_AVG_PRICE", 0.0)) # 💡 서브계좌 가동을 위해 시크릿에 '80.00' 입력 추천
BASE_AMOUNT = float(os.environ.get("BASE_AMOUNT", 0.0))     
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  
CHAT_ID = os.environ.get("CHAT_ID")           

# 🚨 [양도세 스위치] TRUE면 매도 신호를 강제로 잠그고 홀딩합니다.
TAX_FREE_EXHAUSTED = os.environ.get("TAX_FREE_EXHAUSTED", "FALSE").upper() == "TRUE"

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 설정이 누락되었습니다.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"메시지 전송 실패: {e}")

def run_rst_strategy():
    ticker = "TQQQ"
    # 최근 3개월 치 일봉 데이터를 가져옵니다.
    df = yf.download(ticker, period="3mo", interval="1d")
    if df.empty:
        send_telegram_message("❌ TQQQ 데이터를 가져오지 못했습니다.")
        return
        
    # 5일선, 20일선 계산
    df['SMA5'] = df['Close'].rolling(window=5).mean()
    df['SMA20'] = df['Close'].rolling(window=20).mean()
    
    current_price = float(df['Close'].iloc[-1])
    yesterday_price = float(df['Close'].iloc[-2])
    sma5 = float(df['SMA5'].iloc[-1])
    sma20 = float(df['SMA20'].iloc[-1])
    
    # ------------------------------------------------
    # 📊 [유저 요청 반영] 시장 추세 진단 텍스트 생성 로직
    # ------------------------------------------------
    trend_diagnose = ""
    # 5일선과 20일선의 정배열/역배열 판단
    if sma5 > sma20:
        trend_diagnose += "🔥 **단기 정배열 상승 세력 우세** (5일선 > 20일선)\n"
    else:
        trend_diagnose += "❄️ **역배열 하락/조정 국면 진입** (5일선 <= 20일선)\n"
        
    # 현재 주가의 위치와 방향성 분석
    if current_price > sma20:
        trend_diagnose += "📈 주가가 20일선 위에서 안정적으로 지지받으며 순항 중입니다."
    else:
        if current_price > yesterday_price:
            trend_diagnose += "🩹 20일선 아래 침체기이나, 전일 대비 **단기 반등(양봉)**을 시도하고 있습니다."
        else:
            trend_diagnose += "📉 20일선 아래에서 **추가 추락 중**입니다. (위험! 떨어지는 칼날 구간)"

    # 평단가 대비 하락률 계산
    drop_rate = ((current_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0
    
    # 리포트 상단 기본 정보 구성
    msg_lines = [
        "🤖 **[RST-Trend 알림]** 오늘 밤 매매 가이드",
        f"📊 TQQQ 종가: `${current_price:.2f}` (전일: `${yesterday_price:.2f}`)",
        f"📈 5일선: `${sma5:.2f}` | 20일선: `${sma20:.2f}`",
        f"🍏 기준 평단가: `${MY_AVG_PRICE:.2f}` | 평단대비: `{drop_rate:.1f}%`",
        f"💰 잔여 예수금: `${MY_CASH:.2f}`",
        "─" * 15,
        "🔍 **[현재 시장 추세 분석]**",
        trend_diagnose,
        "─" * 15
    ]
    
    # 🟢 [구간 1] 현재가 < 내 평단가 ➔ 하락장 매수 타점 탐색
    if current_price < MY_AVG_PRICE:
        # 규칙 ①: 20일선 아래에 있으면서 + 오늘 종가가 전일보다 오른 '반등' 상태일 때만 매수
        is_below_sma20 = current_price < sma20
        is_rebounding = current_price > yesterday_price
        
        if is_below_sma20 and is_rebounding:
            # 규칙 ② + [안전장치 1]: 비선형 가속 분할 및 최대 8배 상한선 고정
            if drop_rate <= -30: multiplier = 8
            elif drop_rate <= -20: multiplier = 4
            elif drop_rate <= -10: multiplier = 2
            else: multiplier = 1
            
            required_cash = BASE_AMOUNT * multiplier
            shares_to_buy = math.floor(required_cash / current_price)
            
            # [안전장치 2]: 예수금 고갈 가드
            if MY_CASH <= 0:
                msg_lines.append("📢 **[오늘 밤 주문]** 매수 조건은 맞으나 **예수금 전액 고갈**로 **【 강제 관망 】**")
            elif MY_CASH < required_cash:
                final_cash = MY_CASH
                shares_to_buy = math.floor(final_cash / current_price)
                msg_lines.append(f"⚠️ 예수금이 모자라 남은 잔액 `${MY_CASH:.0f}` 전액만 매수 처리합니다.")
                msg_lines.append(f"🛒 **[오늘 밤 주문]** 서브 계좌 LOC 매수: **【 {shares_to_buy}주 】**")
            else:
                msg_lines.append(f"🛒 **[오늘 밤 주문]** 서브 계좌 LOC 매수: 🚨 **【 {shares_to_buy}주 】** ({multiplier}배 가속펌핑)")
        else:
            msg_lines.append("📢 **[오늘 밤 주문]** 하락 추세가 멈추지 않았거나 반등 기미가 없어 **【 관망 (칼날 회피) 】** 🛡️")
            
    # 🔴 [구간 2] 현재가 >= 내 평단가 ➔ 익절 타점 탐색
    else:
        # 상황 A: 올해 양도세 면세 한도 소진 상태 (스위치 ON 일 때)
        if TAX_FREE_EXHAUSTED:
            msg_lines.append("📢 **[오늘 밤 주문]** 올해 양도세 한도 소진 상태이므로 세금 방어를 위해 **【 무조건 홀딩 】** 🔒")
            msg_lines.append("\n💡 *팁: 하락장 매수 신호 발생 시에는 '서브 계좌'를 활용해 분할 매수하세요!*")
        
        # 상황 B: 내년이 되어 양도세 스위치를 껐을 때 (스위치 OFF 일 때)
        else:
            # 규칙 ③: 평단 위 + 상승 추세(5일선 > 20일선)일 때는 매도 제한 (5일선 깨지면 분할 매도)
            is_selling_signal = (current_price < sma5) or (sma5 < sma20)
            
            if is_selling_signal:
                shares_to_sell = math.floor(MY_SHARES * 0.1) # 10% 분할 익절 예시
                if shares_to_sell > 0:
                    msg_lines.append("🔍 분석: 평단 이상 구간에서 단기 상승 추세 이탈 확인 (5일선 붕괴)")
                    msg_lines.append(f"🚨 **[오늘 밤 주문]** 메인 계좌 지정가 매도: 💰 **【 {shares_to_sell}주 분할 익절 】**")
                else:
                    msg_lines.append("📢 **[오늘 밤 주문]** 매도할 수량이 부족하여 **【 보유 유지 】**")
            else:
                msg_lines.append("🔍 분석: 평단 이상 구간이며, 상승 추세가 짱짱하게 유지 중입니다.")
                msg_lines.append("📢 **[오늘 밤 주문]** 수익 극대화를 위해 매도 없이 **【 즐겁게 홀딩 】** 📈")

    send_telegram_message("\n".join(msg_lines))

if __name__ == "__main__":
    run_rst_strategy()
