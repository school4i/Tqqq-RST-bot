import os
import math
import requests
import yfinance as yf
import pandas as pd

# ⚙️ [환경변수] 깃허브 시크릿 금고에서 값을 읽어옵니다.
MY_SHARES = int(os.environ.get("MY_SHARES", 0))          
MY_CASH = float(os.environ.get("MY_CASH", 0.0))          
MY_AVG_PRICE = float(os.environ.get("MY_AVG_PRICE", 0.0)) 
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
    df = yf.download(ticker, period="3mo", interval="1d")
    if df.empty:
        send_telegram_message("❌ TQQQ 데이터를 가져오지 못했습니다.")
        return
        
    df['SMA5'] = df['Close'].rolling(window=5).mean()
    df['SMA20'] = df['Close'].rolling(window=20).mean()
    
    current_price = float(df['Close'].iloc[-1])
    sma5 = float(df['SMA5'].iloc[-1])
    sma20 = float(df['SMA20'].iloc[-1])
    
    msg_lines = [
        "🤖 **[RST-Trend 알림]** 오늘 밤 매매 가이드",
        f"📊 현재가: ${current_price:.2f} (5일선: ${sma5:.2f} / 20일선: ${sma20:.2f})",
        f"🍏 내 평단가: ${MY_AVG_PRICE:.2f} | 보유 수량: {MY_SHARES}주",
        "---"
    ]
    
    # 🟢 [구간 1] 현재가 < 내 평단가 ➔ 매수 타점 노리는 구간
    if current_price < MY_AVG_PRICE:
        is_buying_signal = (current_price > sma5) and (df['Close'].iloc[-1] > df['Open'].iloc[-1])
        
        if is_buying_signal:
            drop_rate = (MY_AVG_PRICE - current_price) / MY_AVG_PRICE
            if drop_rate >= 0.30: multiplier = 8
            elif drop_rate >= 0.20: multiplier = 4
            elif drop_rate >= 0.10: multiplier = 2
            else: multiplier = 1
            
            required_cash = BASE_AMOUNT * multiplier
            shares_to_buy = math.floor(required_cash / current_price)
            
            if MY_CASH >= required_cash and shares_to_buy > 0:
                msg_lines.append(f"🔍 분석: 평단 이하 + 반등 시그널 포착 (매수 강도 {multiplier}배)")
                msg_lines.append(f"🛒 **[오늘 밤 주문]** **【 {shares_to_buy}주 매수 】** (약 ${required_cash:.0f}치)")
            else:
                msg_lines.append("🔍 분석: 매수 신호가 떴으나 예수금이 부족합니다.")
                msg_lines.append("📢 **[오늘 밤 주문]** **【 홀딩 (예수금 보호) 】** 🔒")
        else:
            msg_lines.append("🔍 분석: 평단 이하 구간이나, 아직 하락세가 멈추지 않았습니다.")
            msg_lines.append("📢 **[오늘 밤 주문]** 매수하지 않고 **【 홀딩 】** 합니다. 🛡️")
            
    # 🔴 [구간 2] 현재가 >= 내 평단가 ➔ 익절 타점 노리는 구간
    else:
        # 상황 A: 올해 양도세 한도가 이미 소진되어 매도를 막아야 할 때 (스위치 ON)
        if TAX_FREE_EXHAUSTED:
            msg_lines.append("🔍 분석: 평단 이상 구간입니다. (올해 양도세 한도 소진 상태)")
            msg_lines.append("📢 **[오늘 밤 주문]** 세금 방어를 위해 매도 신호는 무시하고 **【 무조건 홀딩 】** 합니다. 🔒")
            msg_lines.append("\n💡 *팁: 하락장 매수 신호 발생 시에는 '서브 계좌'를 활용해 분할 매수하세요!*")
        
        # 상황 B: 내년이 되어 양도세가 리셋되었을 때 (스위치 OFF)
        else:
            is_selling_signal = (current_price < sma5) or (sma5 < sma20)
            
            if is_selling_signal:
                # 안전하게 보유 수량의 10%씩 분할 매도하는 예시 로직
                shares_to_sell = math.floor(MY_SHARES * 0.1)
                if shares_to_sell > 0:
                    msg_lines.append("🔍 분석: 평단 이상 + 상승 추세 이탈 (5일선 붕괴)")
                    msg_lines.append(f"🚨 **[오늘 밤 주문]** **【 {shares_to_sell}주 분할 매도 】** 💰")
                else:
                    msg_lines.append("📢 **[오늘 밤 주문]** **【 홀딩 】** (매도할 수량이 부족합니다.)")
            else:
                msg_lines.append("🔍 분석: 평단 이상 구간이나, 상승 추세가 짱짱하게 유지 중입니다.")
                msg_lines.append("📢 **[오늘 밤 주문]** 수익 극대화를 위해 **【 즐겁게 홀딩 】** 📈")

    send_telegram_message("\n".join(msg_lines))

if __name__ == "__main__":
    run_rst_strategy()
