import os
import math
import requests
import yfinance as yf
import pandas as pd

# ⚙️ [완벽 보안] 깃허브 시크릿 금고에서 값을 읽어옵니다. (기본값 0)
MY_SHARES = int(os.environ.get("MY_SHARES", 0))          
MY_CASH = float(os.environ.get("MY_CASH", 0.0))          
MY_AVG_PRICE = float(os.environ.get("MY_AVG_PRICE", 0.0)) 
BASE_AMOUNT = float(os.environ.get("BASE_AMOUNT", 0.0))     
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  
CHAT_ID = os.environ.get("CHAT_ID")           

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
    # TQQQ 데이터 가져오기
    ticker = "TQQQ"
    df = yf.download(ticker, period="3mo", interval="1d")
    if df.empty:
        send_telegram_message("❌ TQQQ 데이터를 가져오지 못했습니다.")
        return
        
    # 이동평균선 계산 (Squeeze 제거를 위해 데이터 추출)
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
    
    # [상황 A] 현재가 < 내 평단가 -> 매수 구간
    if current_price < MY_AVG_PRICE:
        is_buying_signal = (current_price > sma5) and (df['Close'].iloc[-1] > df['Open'].iloc[-1])
        
        if is_buying_signal:
            # 비선형 분할 매수 강도 계산 (예시: 평단 대비 낙폭 기준)
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
                msg_lines.append("🔍 분석: 매수 신호가 떴으나 예수금이 부족하거나 수량이 모자랍니다.")
                msg_lines.append("📢 **[오늘 밤 주문]** **【 홀딩 (예수금 보호) 】** 🔒")
        else:
            msg_lines.append("🔍 분석: 평단 이하 구간이나, 아직 하락세가 멈추지 않았습니다 (칼날 회피).")
            msg_lines.append("📢 **[오늘 밤 주문]** 매수하지 않고 **【 홀딩 】** 합니다. 🛡️")
            
    # [상황 B] 현재가 >= 내 평단가 -> 매도 구간 (유저 요청: 양도세 방어로 무조건 무시/홀딩 가이드)
    else:
        msg_lines.append("🔍 분석: 평단 이상 구간입니다. (올해 양도세 한도 소진 상태)")
        msg_lines.append("📢 **[오늘 밤 주문]** 세금 방어를 위해 매도 신호는 무시하고 **【 무조건 홀딩 】** 합니다. 🔒")
        msg_lines.append("\n💡 *팁: 하락장 매수 신호 발생 시에는 '서브 계좌'를 활용해 분할 매수하세요!*")

    send_telegram_message("\n".join(msg_lines))

if __name__ == "__main__":
    run_rst_strategy()
