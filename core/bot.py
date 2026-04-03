# Main trading bot logic

from core.market import get_market_data
from news.news_pipeline import get_news_and_sentiment
from core.strategy import should_trade
from core.risk import calculate_lot_size
from brokers.deriv_live import place_trade as deriv_place_trade

try:
    from brokers.mt5_live import place_trade as mt5_place_trade
except ImportError:
    mt5_place_trade = None

ACCOUNT_BALANCE = 10000  # Example balance
RISK_PER_TRADE = 0.01    # 1% risk
STOP_LOSS_PIPS = 50      # Example stop loss
PIP_VALUE = 1            # Example pip value for XAU/USD
USE_MT5 = False  # Runtime environment default; explicitly enable if MT5 is supported


def run_trading_cycle():
    market_data = get_market_data()
    articles, sentiment = get_news_and_sentiment()
    trade_decision, reason = should_trade(market_data, sentiment)
    if trade_decision:
        lot_size = calculate_lot_size(ACCOUNT_BALANCE, RISK_PER_TRADE, STOP_LOSS_PIPS, PIP_VALUE)
        print(f"Placing trade: lot size {lot_size:.2f} | Reason: {reason}")
        if USE_MT5:
            if mt5_place_trade is None:
                raise RuntimeError("MT5 trading is not available because MetaTrader5 package is missing")
            result = mt5_place_trade('buy', lot_size)  # Example: always buy
            print(f"MT5 trade result: {result}")
        else:
            result = deriv_place_trade('buy', lot_size)
            print(f"Deriv trade result: {result}")
    else:
        print(f"No trade: {reason}")

if __name__ == "__main__":
    run_trading_cycle()
