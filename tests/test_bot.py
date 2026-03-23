# Test for main trading bot logic

from core.bot import run_trading_cycle

def test_run_trading_cycle():
    run_trading_cycle()
    print("Trading cycle ran successfully.")

if __name__ == "__main__":
    test_run_trading_cycle()
