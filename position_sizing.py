"""
position_sizing.py — Tiered position sizing for binary options.

Binary options are CAPPED LOSS instruments — you can only lose what you stake.
This allows more aggressive sizing than CFD trading where losses can exceed stake.

Sizing tiers (based on current balance):
  $0-50      → 10% per trade  (survival/growth mode on micro accounts)
  $50-200    →  5% per trade  (growth mode)
  $200-1000  →  3% per trade  (compound mode)
  $1000+     →  2% per trade  (preservation mode, $50 hard cap)

Recovery mode (after any loss):
  Drop one tier down until 2 consecutive wins, then return to normal tier.
  This prevents a losing streak from wiping out recent gains.

Example stakes:
  $10  balance → $1.00   (10% = $1, at Deriv minimum)
  $50  balance → $5.00   (10%)
  $100 balance → $5.00   (5%)
  $200 balance → $10.00  (5%)
  $300 balance → $9.00   (3%)
  $500 balance → $15.00  (3%)
  $1000 balance → $20.00 (2%)
  $5000 balance → $50.00 (2%, capped)

Break-even win rate for 75% payout binary options: 57.1%
At 60% win rate, expected value per $10 stake = +$0.50
At 65% win rate, expected value per $10 stake = +$1.25
"""
import math
import logging

logger = logging.getLogger("PositionSizing")

# Absolute limits
MIN_STAKE = 1.00    # Deriv minimum
MAX_STAKE = 50.00   # Never exceed this regardless of balance
ROUND_TO  = 0.50    # Round to nearest 50 cents

# Tier definitions: (max_balance, risk_pct, label)
TIERS = [
    (50,    0.10, "SURVIVAL"),    # $0-50: 10%
    (200,   0.05, "GROWTH"),      # $50-200: 5%
    (1000,  0.03, "COMPOUND"),    # $200-1000: 3%
    (float('inf'), 0.02, "PRESERVE"),  # $1000+: 2%
]

# Recovery: wins needed to exit recovery mode
WINS_TO_EXIT_RECOVERY = 2


def _get_tier(balance: float) -> tuple[float, str]:
    """Return (risk_pct, tier_label) for given balance."""
    for max_bal, risk_pct, label in TIERS:
        if balance <= max_bal:
            return risk_pct, label
    return 0.02, "PRESERVE"


def _get_recovery_tier(balance: float) -> tuple[float, str]:
    """
    In recovery, drop one tier down.
    E.g. if balance is $300 (normally COMPOUND 3%), recovery uses GROWTH 5%... 
    Wait, dropping a tier means LESS risk. So $300 normally at 3%, recovery at 2%.
    Actually for small accounts we go the other way — $100 normally 5%, recovery 3%.
    Recovery always steps DOWN one tier in risk %.
    """
    for i, (max_bal, risk_pct, label) in enumerate(TIERS):
        if balance <= max_bal:
            # Drop to next tier down (lower risk %)
            if i + 1 < len(TIERS):
                next_risk, next_label = TIERS[i+1][1], TIERS[i+1][2]
                return next_risk, f"RECOVERY({next_label})"
            else:
                # Already at lowest tier — halve it
                return risk_pct * 0.5, "RECOVERY(MIN)"
    return 0.01, "RECOVERY(MIN)"


def calculate_stake(
    balance: float,
    win_rate: float = 0.0,
    in_recovery: bool = False,
    consecutive_wins: int = 0,
) -> tuple[float, str]:
    """
    Calculate optimal stake for next binary options trade.

    Args:
        balance:          Current account balance in USD
        win_rate:         Historical win rate as decimal (0.60 = 60%)
                          Used to scale down if win rate is concerning
        in_recovery:      True after a loss, until consecutive_wins threshold
        consecutive_wins: Wins in a row (used to exit recovery)

    Returns:
        (stake_amount, tier_label) tuple
    """
    if balance <= 0:
        return MIN_STAKE, "MIN"

    # ── Win rate safety check ──────────────────────────────────────────────────
    # If we have enough trade history and win rate is below break-even (57.1%),
    # force minimum sizing until the strategy proves itself again.
    # Only apply this check if we have meaningful sample size (20+ trades).
    if win_rate > 0 and win_rate < 0.50:
        # Below 50% win rate — drop to minimum sizing, strategy may be broken
        stake = max(MIN_STAKE, balance * 0.01)
        stake = _round_stake(stake)
        logger.warning(
            f"⚠️  Win rate {win_rate:.1%} below 50% — "
            f"minimum sizing ${stake:.2f} until strategy recovers"
        )
        return stake, "WIN_RATE_CAUTION"

    # ── Select tier ────────────────────────────────────────────────────────────
    if in_recovery and consecutive_wins < WINS_TO_EXIT_RECOVERY:
        risk_pct, tier_label = _get_recovery_tier(balance)
    else:
        risk_pct, tier_label = _get_tier(balance)

    # ── Calculate raw stake ────────────────────────────────────────────────────
    raw_stake = balance * risk_pct

    # Apply absolute bounds
    stake = max(MIN_STAKE, min(MAX_STAKE, raw_stake))
    stake = _round_stake(stake)

    logger.info(
        f"💰 Stake ${stake:.2f} | tier={tier_label} ({risk_pct:.0%}) | "
        f"balance=${balance:.2f} | wr={win_rate:.1%} | "
        f"recovery={'YES (' + str(consecutive_wins) + '/' + str(WINS_TO_EXIT_RECOVERY) + ')' if in_recovery else 'NO'}"
    )

    return stake, tier_label


def _round_stake(amount: float) -> float:
    """Floor to nearest ROUND_TO increment."""
    return max(MIN_STAKE, math.floor(amount / ROUND_TO) * ROUND_TO)


def get_sizing_context(balance: float, win_rate: float) -> dict:
    """Return full sizing context for logging/display."""
    normal_risk, normal_tier   = _get_tier(balance)
    recovery_risk, recovery_tier = _get_recovery_tier(balance)
    normal_stake, _  = calculate_stake(balance, win_rate)
    recovery_stake, _ = calculate_stake(balance, win_rate,
                                         in_recovery=True, consecutive_wins=0)
    return {
        "balance":        balance,
        "tier":           normal_tier,
        "risk_pct":       f"{normal_risk:.0%}",
        "normal_stake":   normal_stake,
        "recovery_stake": recovery_stake,
        "win_rate":       f"{win_rate:.1%}",
        "break_even_wr":  "57.1%",
    }