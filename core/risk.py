# Risk management module

def calculate_lot_size(account_balance, risk_per_trade, stop_loss_pips, pip_value):
    """
    Calculate lot size based on risk management rules.
    """
    risk_amount = account_balance * risk_per_trade
    lot_size = risk_amount / (stop_loss_pips * pip_value)
    return max(lot_size, 0.01)  # Minimum lot size 0.01
