import asyncio
import sqlite3
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
        self.db_path = "/tmp/users.db"  # Ensure this matches user_models.py path

    def save_signal(self, signal_type, price, rsi, bias, reason):
        """Writes bot analysis to the DB so the Mobile App can display it."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO signals (symbol, type, price, rsi, bias, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.symbol, signal_type, price, rsi, bias, reason),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ DB Signal Error: {e}")

    async def get_recent_candles(self, service):
        """
        Fetches the last 250 candles to calculate accurate indicators.
        Extra padding ensures EMA 200 has sufficient history.
        """
        raw_candles = await service.get_candles(
            self.symbol, count=250, granularity=300
        )

        if not raw_candles:
            return pd.DataFrame()

        df = pd.DataFrame(raw_candles)
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["open"] = df["open"].astype(float)
        return df

    def analyze_technicals(self, df):
        """
        Calculates Trend (EMA 200), Momentum (RSI 14), and Volatility (ATR 14).
        """
        if df.empty or len(df) < 200:
            return pd.DataFrame()

        # 1. Trend: 200-period Exponential Moving Average
        df["EMA_200"] = EMAIndicator(close=df["close"], window=200).ema_indicator()

        # 2. Momentum: 14-period Relative Strength Index
        df["RSI_14"] = RSIIndicator(close=df["close"], window=14).rsi()

        # 3. Volatility: 14-period Average True Range
        df["ATRr_14"] = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14
        ).average_true_range()

        # Drop rows without enough history to populate EMA 200
        df.dropna(inplace=True)
        return df

    async def execute_trade_cycle(self):
        service = DerivTradingService()
        try:
            await service.authenticate()
            print(f"[{datetime.now()}] 🤖 Scanning XAU/USD...")

            # --- PILLAR 1: SENTIMENT ---
            _, sentiment_data = get_news_and_sentiment()
            market_bias = sentiment_data.get("overall", "Neutral")

            # --- PILLAR 2: TECHNICALS & VOLATILITY ---
            df = await self.get_recent_candles(service)
            df = self.analyze_technicals(df)

            # --- SAFETY CHECK: Not enough candle history yet ---
            if df.empty:
                self.save_signal(
                    "NEUTRAL", 0, 0, market_bias, "Collecting historical data..."
                )
                print("⏳ Not enough market data yet (EMA 200 requires more history). Skipping...")
                return

            current = df.iloc[-1]
            price   = current["close"]
            ema_200 = current["EMA_200"]
            rsi     = current["RSI_14"]
            atr     = current["ATRr_14"]

            print(
                f"📊 Stats | Price: {price} | RSI: {round(rsi, 2)} "
                f"| ATR: {round(atr, 2)} | Bias: {market_bias}"
            )

            # --- RISK MANAGEMENT OVERRIDE ---
            MAX_SAFE_ATR = 2.5
            if atr > MAX_SAFE_ATR:
                reason = f"Volatility too high (ATR {round(atr, 2)} > {MAX_SAFE_ATR})"
                self.save_signal("NEUTRAL", price, rsi, market_bias, reason)
                print(f"⚠️ {reason}. Aborting trade cycle.")
                return

            # --- DECISION LOGIC ---
            if price > ema_200 and rsi < 35 and market_bias != "Bearish":
                reason = "Trend Up + Oversold + Positive Sentiment"
                self.save_signal("BUY", price, rsi, market_bias, reason)
                print("🟢 CONFLUENCE MET: Executing LONG (CALL) Order!")
                await service.place_order("CALL", self.trade_amount, 3, self.symbol)

            elif price < ema_200 and rsi > 65 and market_bias != "Bullish":
                reason = "Trend Down + Overbought + Negative Sentiment"
                self.save_signal("SELL", price, rsi, market_bias, reason)
                print("🔴 CONFLUENCE MET: Executing SHORT (PUT) Order!")
                await service.place_order("PUT", self.trade_amount, 3, self.symbol)

            else:
                # Build a specific reason so the mobile app can surface it
                if rsi > 65:
                    reason = "RSI Overbought, but Trend is Up (Waiting for drop)"
                elif rsi < 35:
                    reason = "RSI Oversold, but Trend is Down (Dangerous)"
                else:
                    reason = "Waiting for RSI/Trend alignment"

                self.save_signal("NEUTRAL", price, rsi, market_bias, reason)
                print(f"⏳ Neutral: {reason}")

        except Exception as e:
            print(f"❌ Strategy Error: {e}")
        finally:
            await service.close()

    async def start_bot_loop(self):
        """Runs the strategy continuously every 5 minutes."""
        self.is_running = True
        while self.is_running:
            await self.execute_trade_cycle()
            await asyncio.sleep(300)