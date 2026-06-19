import os
import math
import requests
import yfinance as yf
import pandas as pd

# ====================================================
# ⚙️ [완벽 보안] 깃허브 시크릿 금고에서 값을 읽어옵니다.
# ====================================================
INITIAL_CASH = float(os.environ.get("INITIAL_CASH", 22555.0)) # 🌟 최초 시작 현금 (구간별 한도 계산용)
MY_CASH = float(os.environ.get("MY_CASH", 0.0))          
MY_SHARES = int(os.environ.get("MY_SHARES", 0))          
MY_AVG_PRICE = float(os.environ.get("MY_AVG_PRICE", 0.0)) # 💡 서브계좌 가동을 위해 시크릿에 '80.00' 입력 추천
BASE_AMOUNT = float(os.environ.get("BASE_AMOUNT", 0.0))     
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  
CHAT_ID = os.environ.get("CHAT_ID")           

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
    # RSI(14일) 계산을 위해 안전하게 6개월치 데이터를 가져옵니다.
    df = yf.download(ticker, period="6mo", interval="1d")
    if df.empty:
        send_telegram_message("❌ TQQQ 데이터를 가져오지 못했습니다.")
        return
        
    # 🌟 [정밀 타격] 종가(Close)가 없는 휴일 데이터를 깨끗하게 지웁니다.
    df = df[df['Close'].notna()]
        
    # 1. 기술적 지표 계산 (이평선)
    df['SMA5'] = df['Close'].rolling(window=5).mean()
    df['SMA20'] = df['Close'].rolling(window=20).mean()
    
    # 2. 순수 파이썬 기반 RSI(14) 계산 로직 (TA-Lib 미설치 대응)
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 당일 및 전일 데이터 추출
    current_price = float(df['Close'].iloc[-1])
    open_price = float(df['Open'].iloc[-1])
    yesterday_price = float(df['Close'].iloc[-2])
    
    sma5_current = float(df['SMA5'].iloc[-1])
    sma5_yesterday = float(df['SMA5'].iloc[-2])
    sma20_current = float(df['SMA20'].iloc[-1])
    rsi_current = float(df['RSI'].iloc[-1])
    
    # 평단가 대비 하락률 계산
    drop_rate = ((current_price - MY_AVG_PRICE) / MY_AVG_PRICE) * 100 if MY_AVG_PRICE > 0 else 0
    # 지금까지 사용한 총 누적 현금
    spent_cash = INITIAL_CASH - MY_CASH

    # ------------------------------------------------
    # 🔍 시장 추세 진단 및 지표 브리핑 생성
    # ------------------------------------------------
    trend_diagnose = ""
    if sma5_current > sma20_current:
        trend_diagnose += "🔥 **단기 정배열 상승세 유지** (5일선 > 20일선)\n"
    else:
        trend_diagnose += "❄️ **역배열 하락/조정 국면 진행 중** (5일선 <= 20일선)\n"
        
    if current_price > sma20_current:
        trend_diagnose += "📈 주가가 20일선 위 쾌적한 구간에 있습니다."
    else:
        # 유저님의 4중 매수 필터 조건 체크 변수들
        is_below_20 = current_price < sma20_current
        is_yang_bong = current_price > open_price
        is_rsi_low = rsi_current <= 30
        is_sma5_up = sma5_current > sma5_yesterday
        
        if is_below_20 and is_yang_bong and is_rsi_low and is_sma5_up:
            trend_diagnose += "🚨 **[대박 타점] 유저의 4중 매수 필터가 모두 정렬되었습니다! (진바닥 확률 최상)**"
        else:
            reasons = []
            if not is_yang_bong: reasons.append("음봉 상태")
            if not is_rsi_low: reasons.append(f"RSI({rsi_current:.1f})가 30보다 높음")
            if not is_sma5_up: reasons.append("5일선 우하향 중")
            trend_diagnose += f"📉 20일선 아래이나 가짜 반등 경계 중 (미충족 요인: {', '.join(reasons)})"

    msg_lines = [
        "🤖 **[RST-Trend v2 알림]** 매매 가이드",
        f"📊 TQQQ 종가: `${current_price:.2f}` (RSI: `{rsi_current:.1f}`)",
        f"📈 5일선: `${sma5_current:.2f}` (전일: `${sma5_yesterday:.2f}`)",
        f"📉 20일선: `${sma20_current:.2f}`",
        f"🍏 기준 평단가: `${MY_AVG_PRICE:.2f}` | 평단대비: `{drop_rate:.1f}%`",
        f"💰 잔여 예수금: `${MY_CASH:.2f}` (소진율: `{(spent_cash/INITIAL_CASH)*100:.1f}%`)",
        "─" * 15,
        "🔍 **[현재 시장 추세 분석]**",
        trend_diagnose,
        "─" * 15
    ]
    
    # 🟢 [구간 1] 현재가 < 내 평단가 ➔ 하락장 구간
    if current_price < MY_AVG_PRICE:
        
        # 📊 [유저 제안 반영] 현금 방어벽 리스크 통제 (Circuit Breaker)
        zone_num = 0
        allowed_spent_ratio = 0.0
        zone_name = ""
        
        if 0 >= drop_rate > -20:
            zone_num = 1; allowed_spent_ratio = 0.20; zone_name = "구간 1 (0 ~ -20%) [현금한도 20%]"
        elif -20 >= drop_rate > -40:
            zone_num = 2; allowed_spent_ratio = 0.50; zone_name = "구간 2 (-20 ~ -40%) [누적한도 50%]" # 20% + 30%
        elif -40 >= drop_rate > -60:
            zone_num = 3; allowed_spent_ratio = 0.80; zone_name = "구간 3 (-40 ~ -60%) [누적한도 80%]" # 50% + 30%
        else:
            zone_num = 4; allowed_spent_ratio = 1.00; zone_name = "구간 4 (-60% 이하) [현금한도 100%]"

        max_allowed_cash_spent = INITIAL_CASH * allowed_spent_ratio
        
        # 현재 구간에서 현금을 이미 다 썼는지 체크
        if spent_cash >= max_allowed_cash_spent:
            msg_lines.append(f"🔍 분석: 현재 주가는 *{zone_name}* 에 위치합니다.")
            msg_lines.append("📢 **[오늘 밤 주문]** 해당 구간에 할당된 현금을 모두 소진하여 **【 강제 서킷 브레이커 (매수 잠금) 】** 합니다. 🛡️")
            
        else:
            # 🔥 [유저 제안 반영] 완벽한 4중 매수 필터 발동
            is_below_20 = current_price < sma20_current
            is_yang_bong = current_price > open_price
            is_rsi_low = rsi_current <= 30
            is_sma5_up = sma5_current > sma5_yesterday
            
            if is_below_20 and is_yang_bong and is_rsi_low and is_sma5_up:
                # 비선형 가속 승수 지정
                if zone_num == 4: multiplier = 8
                elif zone_num == 3: multiplier = 4
                elif zone_num == 2: multiplier = 2
                else: multiplier = 1
                
                required_cash = BASE_AMOUNT * multiplier
                shares_to_buy = math.floor(required_cash / current_price)
                
                # 예수금 최종 검증 (Cash Guard)
                if MY_CASH <= 0:
                    msg_lines.append("📢 **[오늘 밤 주문]** 4중 필터 만족하나 잔액 고갈로 **【 관망 】**")
                elif MY_CASH < required_cash:
                    shares_to_buy = math.floor(MY_CASH / current_price)
                    msg_lines.append(f"🛒 **[오늘 밤 주문]** 잔액 전액 털어 서브계좌 LOC 매수: **【 {shares_to_buy}주 】**")
                else:
                    msg_lines.append(f"🛒 **[오늘 밤 주문]** 4중 필터 확증 승인! 서브계좌 LOC 매수: 🚨 **【 {shares_to_buy}주 】** ({multiplier}배 가속)")
            else:
                msg_lines.append("📢 **[오늘 밤 주문]** 4중 필터 조건 미충족 (가짜 반등 우려 요인 감지) ➔ **【 관망 】** ⏱️")
            
    # 🔴 [구간 2] 현재가 >= 내 평단가 ➔ 익절 구간
    else:
        if TAX_FREE_EXHAUSTED:
            msg_lines.append("📢 **[오늘 밤 주문]** 올해 양도세 면세 완료 상태이므로 세금 방어 **【 무조건 홀딩 】** 🔒")
        else:
            is_selling_signal = (current_price < sma5_current) or (sma5_current < sma20_current)
            if is_selling_signal:
                shares_to_sell = math.floor(MY_SHARES * 0.1)
                if shares_to_sell > 0:
                    msg_lines.append(f"🚨 **[오늘 밤 주문]** 메인 계좌 지정가 매도: 💰 **【 {shares_to_sell}주 분할 익절 】**")
                else:
                    msg_lines.append("📢 **[오늘 밤 주문]** 매도 가능 수량 부족으로 **【 보유 유지 】**")
            else:
                msg_lines.append("📢 **[오늘 밤 주문]** 추세 정배열 순항 중이므로 매도 없이 **【 즐겁게 홀딩 】** 📈")

    send_telegram_message("\n".join(msg_lines))

if __name__ == "__main__":
    run_rst_strategy()
