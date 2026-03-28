import asyncio
import sqlite3
import pandas as pd
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

from datetime import datetime
from news.news_pipeline import get_news_and_sentiment
from brokers.deriv_trading_service import DerivTradingService


class XAUMasterStrategy:
    def __init__(self):
        self.symbol = "frxXAUUSD"
        self.trade_amount = 10.0
        self.is_running = False
        self.db_path = "/tmp/users.db"

        # Gold M5 natural ATR range is 5–20+
        # Only abort on genuinely extreme spikes
        self.MAX_SAFE_ATR = 18.0

        # Minimum duration Deriv accepts for frxXAUUSD binary options
        self.TRADE_DURATION = 5  # minutes

    def save_signal(self, signal_type, price, rsi, bias, reason):
        """Writes bot analysis to DB so the mobile app can display it."""
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
        """Fetches last 250 M5 candles for accurate indicator calculation."""
        raw_candles = await service.get_candles(
            self.symbol, count=250, granularity=300
        )
        if not raw_candles:
            return pd.DataFrame()

        df = pd.DataFrame(raw_candles)
        df["close"] = df["close"].astype(float)
        df["high"]  = df["high"].astype(float)
        df["low"]   = df["low"].astype(float)
        df["open"]  = df["open"].astype(float)
        return df

    def analyze_technicals(self, df):
        """Calculates EMA 200, RSI 14, ATR 14."""
        if df.empty or len(df) < 200:
            return pd.DataFrame()

        df["EMA_200"] = EMAIndicator(
            close=df["close"], window=200
        ).ema_indicator()

        df["RSI_14"] = RSIIndicator(
            close=df["close"], window=14
        ).rsi()

        df["ATRr_14"] = AverageTrueRange(
            high=df["high"],
            low=df["low"],
            close=df["close"],
            window=14,
        ).average_true_range()

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

            # --- PILLAR 2: TECHNICALS ---
            df = await self.get_recent_candles(service)
            df = self.analyze_technicals(df)

            if df.empty:
                self.save_signal(
                    "NEUTRAL", 0, 0, market_bias,
                    "Collecting historical data...",
                )
                print("⏳ Not enough market data yet. Skipping...")
                return

            current = df.iloc[-1]
            price   = current["close"]
            ema_200 = current["EMA_200"]
            rsi     = current["RSI_14"]
            atr     = current["ATRr_14"]

            print(
                f"📊 Stats | Price: {price} | RSI: {round(rsi, 2)} "
                f"| ATR: {round(atr, 2)} | Bias: {market_bias} "
                f"| EMA200: {round(ema_200, 2)}"
            )

            # --- RISK MANAGEMENT ---
            if atr > self.MAX_SAFE_ATR:
                reason = (
                    f"Extreme volatility (ATR {round(atr, 2)} > "
                    f"{self.MAX_SAFE_ATR}). Standing by."
                )
                self.save_signal("NEUTRAL", price, rsi, market_bias, reason)
                print(f"⚠️ {reason}")
                return

            # --- DECISION LOGIC ---
            if (
                price > ema_200
                and rsi < 35
                and market_bias != "Bearish"
            ):
                reason = "Trend Up + Oversold RSI + Bullish Sentiment"
                self.save_signal("BUY", price, rsi, market_bias, reason)
                print("🟢 CONFLUENCE MET: Executing LONG (CALL) Order!")
                result = await service.place_order(
                    "CALL", self.trade_amount, self.TRADE_DURATION, self.symbol
                )
                print(f"✅ Trade Result: {result}")

            elif (
                price < ema_200
                and rsi > 65
                and market_bias != "Bullish"
            ):
                reason = "Trend Down + Overbought RSI + Bearish Sentiment"
                self.save_signal("SELL", price, rsi, market_bias, reason)
                print("🔴 CONFLUENCE MET: Executing SHORT (PUT) Order!")
                result = await service.place_order(
                    "PUT", self.trade_amount, self.TRADE_DURATION, self.symbol
                )
                print(f"✅ Trade Result: {result}")

            else:
                # Descriptive reason so mobile app can surface it clearly
                if price < ema_200 and rsi < 35:
                    reason = "RSI Oversold but price below EMA 200 — too risky"
                elif price > ema_200 and rsi > 65:
                    reason = "RSI Overbought but price above EMA 200 — waiting for pullback"
                elif market_bias == "Bearish" and price > ema_200:
                    reason = "Price above EMA 200 but sentiment is Bearish — conflicting"
                elif market_bias == "Bullish" and price < ema_200:
                    reason = "Bullish sentiment but price below EMA 200 — conflicting"
                else:
                    reason = (
                        f"Waiting for RSI extremes "
                        f"(RSI: {round(rsi, 1)}, need <35 or >65)"
                    )

                self.save_signal("NEUTRAL", price, rsi, market_bias, reason)
                print(f"⏳ Neutral: {reason}")

        except Exception as e:
            import traceback
            print(f"❌ Strategy Error: {e}")
            traceback.print_exc()
        finally:
            await service.close()

    async def start_bot_loop(self):
        """Runs the strategy every 5 minutes."""
        self.is_running = True
        while self.is_running:
            await self.execute_trade_cycle()
            await asyncio.sleep(300)