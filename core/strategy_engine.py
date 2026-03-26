import asyncio
import pandas as pd
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

from datetime import datetime
from news.news_pipeline import get_news_and_sentiment

# Import your existing Deriv service
from brokers.deriv_trading_service import DerivTradingService

class XAUMasterStrategy:
    def __init__(self):
        self.symbol = "frxXAUUSD"
        self.trade_amount = 10.0  # Base stake amount in USD
        self.is_running = False

    async def get_recent_candles(self, service):
        """
        Fetches the last 200 candles to calculate accurate indicators.
        """
        raw_candles = await service.get_candles(self.symbol, count=250, granularity=300) # Increased to 250 for EMA padding
        
        if not raw_candles:
            return pd.DataFrame()

        # Convert the raw JSON list into a Pandas DataFrame
        df = pd.DataFrame(raw_candles)
        # Ensure correct data types
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['open'] = df['open'].astype(float)
        return df

    def analyze_technicals(self, df):
        """
        Calculates Trend (EMA), Momentum (RSI), and Volatility (ATR) using 'ta'
        """
        if df.empty or len(df) < 200:
            return pd.DataFrame()

        # 1. Trend: 200 Exponential Moving Average
        df['EMA_200'] = EMAIndicator(close=df['close'], window=200).ema_indicator()
        
        # 2. Momentum: 14-period Relative Strength Index
        df['RSI_14'] = RSIIndicator(close=df['close'], window=14).rsi()
        
        # 3. Volatility: 14-period Average True Range
        df['ATRr_14'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        
        # Drop the initial rows that don't have enough data to calculate the 200 EMA
        df.dropna(inplace=True)
        return df

    async def execute_trade_cycle(self):
        service = DerivTradingService()
        try:
            await service.authenticate()
            
            print(f"[{datetime.now()}] 🤖 AI Bot waking up to scan XAU/USD...")

            # --- PILLAR 1: SENTIMENT ---
            _, sentiment_data = get_news_and_sentiment()
            market_bias = sentiment_data.get("overall", "Neutral")
            
            # --- PILLAR 2: TECHNICALS & VOLATILITY ---
            df = await self.get_recent_candles(service)
            df = self.analyze_technicals(df)
            
            # --- SAFETY CHECK: Fixes the 'out-of-bounds' error ---
            if df.empty:
                print(f"⏳ [{datetime.now()}] Not enough market data yet (EMA 200 requires more history). Skipping...")
                return

            # Get the most recently closed candle
            current = df.iloc[-1]
            current_price = current['close']
            ema_200 = current['EMA_200']
            rsi_14 = current['RSI_14']
            atr_14 = current['ATRr_14']

            print(f"📊 Stats | Price: {current_price} | RSI: {round(rsi_14, 2)} | ATR: {round(atr_14, 2)} | Bias: {market_bias}")

            # --- RISK MANAGEMENT OVERRIDE ---
            MAX_SAFE_ATR = 2.5 
            if atr_14 > MAX_SAFE_ATR:
                print("⚠️ Volatility too high. Market is unsafe. Aborting trade cycle.")
                return

            # --- THE TRADING ALGORITHM ---
            
            # LONG (BUY) CONDITIONS
            if current_price > ema_200 and rsi_14 < 35 and market_bias != "Bearish":
                print("🟢 CONFLUENCE MET: Executing LONG (CALL) Order!")
                await service.place_order(
                    contract_type="CALL",
                    amount=self.trade_amount,
                    duration=3,
                    symbol=self.symbol
                )

            # SHORT (SELL) CONDITIONS
            elif current_price < ema_200 and rsi_14 > 65 and market_bias != "Bullish":
                print("🔴 CONFLUENCE MET: Executing SHORT (PUT) Order!")
                await service.place_order(
                    contract_type="PUT",
                    amount=self.trade_amount,
                    duration=3,
                    symbol=self.symbol
                )
            
            else:
                print("⏳ No optimal setup found. Waiting for next cycle.")

        except Exception as e:
            print(f"❌ Strategy Engine Error: {e}")
        finally:
            await service.close()

    async def start_bot_loop(self):
        """Runs the strategy continuously in the background"""
        self.is_running = True
        while self.is_running:
            await self.execute_trade_cycle()
            # Wait 5 minutes before scanning again
            await asyncio.sleep(300)