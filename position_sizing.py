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

With Multiplier contracts (plan_multiplier_trade below), the amount above is
the dollar RISK per trade (the loss if the stop is hit), and the take-profit
is 1.5x that: a fixed 1:1.5 risk-to-reward ratio.
Break-even win rate at 1:1.5: 40.0%
(Rise/Fall at a 75% payout would need 57.1%; at the 64.6% actually quoted
on 2026-10-16, 60.8%.)
"""
import math
import logging

logger = logging.getLogger("PositionSizing")

# Absolute limits
MIN_STAKE = 1.00    # Deriv minimum
MAX_STAKE = 50.00   # Never exceed this regardless of balance
ROUND_TO  = 0.50    # Round to nearest 50 cents
# Win rate below which sizing drops to minimum. This is the 1:1.5 break-even
# (1 / (1 + 1.5) = 40%); it was 50% when trades were Rise/Fall at a 57.1%
# break-even, which would now trip constantly on a profitable ~50-54% strategy.
WIN_RATE_CAUTION_BELOW = 0.40

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
    atr: float = None,
    atr_ceiling: float = None,
    trade_count: int = 0,
) -> tuple[float, str]:
    """
    Calculate optimal stake for next binary options trade.

    Args:
        balance:          Current account balance in USD
        win_rate:         Historical win rate as decimal (0.60 = 60%)
                          Used to scale down if win rate is concerning
        in_recovery:      True after a loss, until consecutive_wins threshold
        consecutive_wins: Wins in a row (used to exit recovery)
        atr:              Current ATR reading, if available
        atr_ceiling:      The strategy's hard volatility veto threshold (trades
                          above this are already blocked before staking is ever
                          computed — see MAX_SAFE_ATR in core/strategy_engine.py).
                          When both are given, stake is scaled down as ATR
                          approaches that ceiling, so the choppy/news-adjacent
                          zone just below the hard cutoff still gets sized more
                          cautiously instead of treated the same as calm markets.
        trade_count:      Total settled trades so far. The win-rate safety
                          check below only engages once this is meaningful
                          (20+) — on a small sample, a dip below break-even is just
                          noise, not evidence the strategy is broken.

    Returns:
        (stake_amount, tier_label) tuple
    """
    if balance <= 0:
        return MIN_STAKE, "MIN"

    # ── Win rate safety check ──────────────────────────────────────────────────
    # If we have enough trade history and win rate looks broken, force minimum
    # sizing until the strategy proves itself again.
    # Bug fixed 2026-08-17: this branch computed balance*1% with no upper
    # bound, so on any balance above ~$2,500 it staked *more* than the
    # normal $50-capped tier — the opposite of what "caution" is supposed to
    # do. On an ~$8,300 balance this staked ~$83/trade, which is exactly what
    # a live trade-log audit caught. Now goes through the same MAX_STAKE cap
    # as every other branch, and only engages past the 20-trade sample size
    # the comment above always said it should (never actually implemented).
    if win_rate > 0 and win_rate < WIN_RATE_CAUTION_BELOW and trade_count >= 20:
        # Below break-even win rate — drop to minimum sizing, strategy may be broken
        stake = max(MIN_STAKE, min(MAX_STAKE, balance * 0.01))
        stake = _round_stake(stake)
        logger.warning(
            f"⚠️  Win rate {win_rate:.1%} below {WIN_RATE_CAUTION_BELOW:.0%} over {trade_count} trades, "
            f"applying minimum sizing ${stake:.2f} until strategy recovers"
        )
        return stake, "WIN_RATE_CAUTION"

    # ── Select tier ────────────────────────────────────────────────────────────
    if in_recovery and consecutive_wins < WINS_TO_EXIT_RECOVERY:
        risk_pct, tier_label = _get_recovery_tier(balance)
    else:
        risk_pct, tier_label = _get_tier(balance)

    # ── Calculate raw stake ────────────────────────────────────────────────────
    raw_stake = balance * risk_pct

    # ── Volatility scaling ─────────────────────────────────────────────────────
    # Linearly cut stake from 100% down to a 50% floor as ATR climbs through the
    # top half of the range below the hard veto ceiling. Below that ceiling every
    # trade currently gets sized identically regardless of how close to it the
    # market actually is — this makes the riskier half of the "still tradeable"
    # range cost less without ever fully zeroing the stake (the hard gate already
    # owns the "too dangerous to trade at all" decision).
    vol_mult = 1.0
    if atr is not None and atr_ceiling:
        atr_ratio = atr / atr_ceiling
        vol_mult  = 1.0 if atr_ratio <= 0.5 else max(0.5, 1.0 - (atr_ratio - 0.5))
        raw_stake *= vol_mult

    # Apply absolute bounds
    stake = max(MIN_STAKE, min(MAX_STAKE, raw_stake))
    stake = _round_stake(stake)

    logger.info(
        f"💰 Stake ${stake:.2f} | tier={tier_label} ({risk_pct:.0%}) | "
        f"balance=${balance:.2f} | wr={win_rate:.1%} | "
        f"vol_mult={vol_mult:.2f} | "
        f"recovery={'YES (' + str(consecutive_wins) + '/' + str(WINS_TO_EXIT_RECOVERY) + ')' if in_recovery else 'NO'}"
    )

    return stake, tier_label


# ── Multiplier (1:1.5 risk-to-reward) trade planning ───────────────────────────
# Rise/Fall pays whatever Deriv quotes (a live quote on 2026-10-16 was 64.6%,
# i.e. 1:0.65), so a fixed 1:1.5 ratio is only possible on Multiplier
# contracts with a take-profit/stop-loss. Deriv's stop_loss / take_profit are
# net dollar P&L *including* commission, so stop_loss=R and take_profit=1.5R
# gives an exact 1:1.5 ratio in money terms.
#
# The amount calculate_stake() returns is used as the dollar RISK R (the loss
# if the stop is hit) — same % of balance a Rise/Fall stake used to risk. The
# Deriv stake is larger than R: it's only lost in full if price gaps straight
# through the stop to Deriv's stop-out level, so it's kept at a minimum of
# MIN_STAKE_TO_RISK x R and that gap exposure is capped at ~2-3.5R.
RR_RATIO             = 1.5    # take_profit = RR_RATIO x stop_loss
SL_ATR_MULT          = 1.0    # stop distance in price = 1x ATR(15M), backtested
# Multipliers a real (demo or live) account may buy on frxXAUUSD, from
# contracts_for and confirmed by a demo buy on 2026-10-19. Deriv's public
# no-auth endpoint *quotes* x50 too, but real accounts reject it ("Accepts
# 100,200,300,500,800"), so never trust public-endpoint quotes for this.
# At x100 a position stops out after ~1% of price, which caps how wide a
# stop can be (see plan_multiplier_trade's feasibility check).
MULTIPLIER_CHOICES   = (100, 200, 300, 500, 800)
MIN_STAKE_TO_RISK    = 2.0    # stake >= 2x risk, so the stop sits well clear of stop-out
COMMISSION_RATE_EST  = 0.00026  # of notional; measured from live frxXAUUSD quotes
                                # ($0.25 on $1000 notional, $0.13 on $500)


# A stake below this multiple of the risk leaves too little room between the
# stop-loss and Deriv's stop-out (loss = whole stake) for a price gap.
MIN_FEASIBLE_STAKE_TO_RISK = 1.25


def plan_multiplier_trade(risk: float, atr: float, price: float,
                          stop_atr_mult: float = SL_ATR_MULT,
                          rr: float | None = RR_RATIO) -> dict:
    """
    Turn a dollar risk amount into Deriv Multiplier contract parameters whose
    stop sits ~stop_atr_mult x ATR from entry. With rr set, take_profit is
    exactly rr x the risk; with rr=None there is no take-profit (the 4H trend
    sleeves exit on a trailing stop instead, see core/trend_strategy.py).

    P&L on a multiplier = notional x (price move / entry) - commission, with
    notional = stake x multiplier. Solving for the notional that loses exactly
    `risk` (commission included) over a stop distance of stop_atr_mult x ATR:
        risk = notional x (stop_dist / price) + notional x commission_rate

    Raises ValueError when the stop is so wide that even the smallest
    multiplier can't keep the stake MIN_FEASIBLE_STAKE_TO_RISK x the risk.
    """
    if risk <= 0 or atr <= 0 or price <= 0:
        raise ValueError(f"Invalid multiplier plan inputs: risk={risk} atr={atr} price={price}")

    stop_dist = stop_atr_mult * atr
    notional  = risk / (stop_dist / price + COMMISSION_RATE_EST)

    # Largest multiplier that still leaves stake >= MIN_STAKE_TO_RISK x risk:
    # a smaller stake means less money exposed to a gap through the stop.
    multiplier = MULTIPLIER_CHOICES[0]
    for m in MULTIPLIER_CHOICES:
        if notional / m >= MIN_STAKE_TO_RISK * risk:
            multiplier = m

    stake = max(MIN_STAKE, round(notional / multiplier, 2))
    if stake * multiplier > 1.2 * notional:
        # Deriv's minimum stake forced a bigger position than planned, which
        # would put the dollar stop_loss far closer than stop_atr_mult x ATR.
        raise ValueError(
            f"Risk ${risk:.2f} too small: the ${MIN_STAKE:.2f} minimum stake at x{multiplier} "
            f"would pull the stop in from {stop_dist:.2f}"
        )
    if stake < MIN_FEASIBLE_STAKE_TO_RISK * risk:
        raise ValueError(
            f"Stop {stop_dist:.2f} too wide for a multiplier: stake ${stake:.2f} "
            f"vs risk ${risk:.2f} even at x{multiplier}"
        )
    return {
        "stake":       stake,
        "multiplier":  multiplier,
        "stop_loss":   round(risk, 2),
        "take_profit": round(risk * rr, 2) if rr else None,
        "notional":    round(stake * multiplier, 2),
    }


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
        "break_even_wr":  "40.0%",
    }