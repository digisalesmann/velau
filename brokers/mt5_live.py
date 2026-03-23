# MT5 live trading integration

import MetaTrader5 as mt5
from config import settings

def connect_mt5():
    if not mt5.initialize(server=settings.MT5_SERVER, login=int(settings.MT5_LOGIN), password=settings.MT5_PASSWORD):
        raise Exception(f"MT5 initialization failed: {mt5.last_error()}")
    return True

def place_trade(direction, lot_size, symbol=settings.PAIR):
    connect_mt5()
    order_type = mt5.ORDER_TYPE_BUY if direction == 'buy' else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": order_type,
        "price": mt5.symbol_info_tick(symbol).ask if direction == 'buy' else mt5.symbol_info_tick(symbol).bid,
        "deviation": 20,
        "magic": 234000,
        "comment": "AI Bot Trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    mt5.shutdown()
    return result
