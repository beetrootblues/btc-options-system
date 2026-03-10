"""BTC Options Quantitative Trading System - Phase 3 Part 1
==========================================================
Core strategy module with Black-Scholes pricing, data classes,
and two fully-backtested option strategies.

Strategies:
  A) Regime-Conditional Vol Selling (Iron Condor in CRISIS)
  B) Momentum Breakout (Long OTM calls/puts on regime transitions)

Author: BTC Options System
Date: 2026-03-09
"""

import math
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm

warnings.filterwarnings("ignore")


# =============================================================================
# 1. BLACK-SCHOLES PRICING MODULE
# =============================================================================

def bs_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Compute d1 in Black-Scholes formula."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Compute d2 in Black-Scholes formula."""
    return bs_d1(S, K, T, r, sigma) - sigma * math.sqrt(max(T, 1e-10))


def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> float:
    """Black-Scholes option price.

    Args:
        S: Spot price
        K: Strike price
        T: Time to expiry in years
        r: Risk-free rate (0 for crypto)
        sigma: Annualised implied volatility (decimal, e.g. 0.60)
        option_type: 'call' or 'put'

    Returns:
        Theoretical option price in USD.
    """
    if T <= 0:
        # At expiry -> intrinsic value
        if option_type == "call":
            return max(S - K, 0.0)
        return max(K - S, 0.0)
    if sigma <= 0 or S <= 0 or K <= 0:
        return 0.0

    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "call":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return max(price, 0.0)


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> Dict[str, float]:
    """Compute Black-Scholes Greeks.

    Returns:
        dict with keys: delta, gamma, theta (per day), vega (per 1% vol move)
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        delta = 1.0 if option_type == "call" and S > K else 0.0
        if option_type == "put":
            delta = -1.0 if S < K else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    sqrt_T = math.sqrt(T)
    pdf_d1 = norm.pdf(d1)
    disc = math.exp(-r * T)

    # Gamma (same for calls and puts)
    gamma = pdf_d1 / (S * sigma * sqrt_T)

    # Vega (per 1% vol move = per 0.01 in sigma)
    vega = S * pdf_d1 * sqrt_T * 0.01

    if option_type == "call":
        delta = norm.cdf(d1)
        theta = (
            -S * pdf_d1 * sigma / (2 * sqrt_T)
            - r * K * disc * norm.cdf(d2)
        ) / 365.0  # per calendar day
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (
            -S * pdf_d1 * sigma / (2 * sqrt_T)
            + r * K * disc * norm.cdf(-d2)
        ) / 365.0

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str = "call",
    tol: float = 1e-8,
    max_iter: int = 100,
    initial_guess: float = 0.50,
) -> float:
    """Newton-Raphson implied volatility solver.

    Args:
        market_price: Observed option premium
        S, K, T, r: Standard BS params
        option_type: 'call' or 'put'
        tol: Convergence tolerance
        max_iter: Maximum iterations
        initial_guess: Starting sigma estimate

    Returns:
        Implied volatility (annualised). Returns NaN on failure.
    """
    if market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return float("nan")

    # Check for intrinsic-value floor
    intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
    if market_price < intrinsic:
        return float("nan")

    sigma = initial_guess
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega_raw = S * norm.pdf(bs_d1(S, K, T, r, sigma)) * math.sqrt(T)

        if vega_raw < 1e-12:
            break

        diff = price - market_price
        if abs(diff) < tol:
            return sigma

        sigma -= diff / vega_raw
        sigma = max(sigma, 1e-6)  # floor at near-zero
        sigma = min(sigma, 10.0)  # cap at 1000%

    # If didn't converge, return best guess if close
    final_price = bs_price(S, K, T, r, sigma, option_type)
    if abs(final_price - market_price) / max(market_price, 1e-6) < 0.01:
        return sigma
    return float("nan")


# =============================================================================
# 2. DATA CLASSES
# =============================================================================

class VolRegime(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRISIS = "CRISIS"

    @property
    def rank(self) -> int:
        """Numeric rank for vol-level comparisons."""
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRISIS": 3}[self.value]

    def __lt__(self, other):
        return self.rank < other.rank

    def __le__(self, other):
        return self.rank <= other.rank

    def __gt__(self, other):
        return self.rank > other.rank

    def __ge__(self, other):
        return self.rank >= other.rank


@dataclass
class OptionLeg:
    """Single leg of an option position."""
    option_type: str          # "call" or "put"
    direction: str            # "long" or "short"
    strike_pct: float         # Strike as fraction of spot (1.15 = 15% OTM call)
    expiry_days: int          # Calendar days to expiry
    size_btc: float           # Position size in BTC
    entry_premium_usd: float = 0.0  # Premium paid/received per BTC

    @property
    def strike_from_spot(self) -> str:
        """Human-readable strike description."""
        otm_pct = abs(self.strike_pct - 1.0) * 100
        side = "OTM" if (
            (self.option_type == "call" and self.strike_pct > 1.0) or
            (self.option_type == "put" and self.strike_pct < 1.0)
        ) else "ITM"
        return f"{otm_pct:.0f}% {side} {self.option_type}"


@dataclass
class TradeSignal:
    """Complete trade signal with all legs, sizing, and risk parameters."""
    strategy_name: str
    timestamp: str
    legs: List[OptionLeg]
    max_loss_usd: float
    confidence: float         # 0-1 scale
    regime: str
    rationale: str
    stop_loss_pct: float = 0.80    # Exit when premium drops to this fraction
    take_profit_pct: float = 3.0   # Exit when premium reaches this multiple
    time_stop_days: int = 21       # Exit when DTE <= this

    @property
    def net_premium(self) -> float:
        """Net premium of the position (positive = credit)."""
        total = 0.0
        for leg in self.legs:
            sign = -1.0 if leg.direction == "long" else 1.0
            total += sign * leg.entry_premium_usd * leg.size_btc
        return total


@dataclass
class TradeResult:
    """Completed trade with P&L."""
    strategy_name: str
    entry_date: str
    exit_date: str
    entry_price_btc: float
    exit_price_btc: float
    pnl_usd: float
    pnl_pct: float           # P&L as % of risk capital
    holding_days: int
    exit_reason: str
    regime_at_entry: str
    equity_before: float
    equity_after: float
    max_premium_value: float = 0.0   # Peak position value during hold


# =============================================================================
# 3. KELLY CRITERION AND POSITION SIZING
# =============================================================================

def half_kelly_fraction(
    win_rate: float,
    avg_win_multiple: float,
    avg_loss_multiple: float = 1.0,
) -> float:
    """Half-Kelly position sizing.

    Kelly f* = (p * b - q) / b
      where p = win_rate, q = 1-p, b = avg_win / avg_loss

    Returns:
        Half-Kelly fraction (capped at sensible range).
    """
    p = win_rate
    q = 1.0 - p
    b = avg_win_multiple / max(avg_loss_multiple, 1e-6)
    kelly = (p * b - q) / b
    half_k = kelly / 2.0
    return max(min(half_k, 0.25), 0.0)  # Cap at 25%, floor at 0


# =============================================================================
# 4. STRATEGY A - REGIME-CONDITIONAL VOL SELLING (Iron Condor)
# =============================================================================

class StrategyA_VolSelling:
    """
    Regime-Conditional Volatility Selling via Iron Condor.

    Thesis: During CRISIS regime, IV is elevated and mean-reverting.
    When VRP z-score shows vol compression starting (recovering toward mean),
    sell iron condors to capture premium decay.

    Entry Conditions (ALL must be true):
      1. regime_threshold == 'CRISIS'
      2. vrp_zscore > -0.5 (VRP recovering -- less negative than deep crisis)
         Note: VRP is structurally negative in CRISIS (RV >> IV). We sell vol
         when VRP starts compressing toward its mean, signaling the crisis peak
         is passing. Original threshold of +0.5 produces ZERO signals because
         VRP never goes positive in CRISIS. -0.5 captures the top ~8% of
         CRISIS VRP readings (53/625 days).
      3. vov_30d is declining (current < 5-day average)
      4. RSI between 30-70 (no extreme momentum)
      5. No active position

    Trade Structure: Iron Condor
      - Sell 15% OTM call + put
      - Buy 25% OTM call + put (wings)
      - 14-day expiry

    Exit Rules:
      - Take profit: 50% of net premium collected
      - Stop loss: 200% of net premium (loss = 2x credit)
      - Time stop: DTE <= 3
      - Regime exit: immediate close if regime changes from CRISIS
      - Kill switch: 3 consecutive losses -> halt 14 days
    """

    NAME = "A_VolSelling_IronCondor"

    # Iron condor parameters
    SHORT_STRIKE_OTM = 0.15     # 15% OTM
    LONG_STRIKE_OTM = 0.25      # 25% OTM (wings)
    EXPIRY_DAYS = 14

    # Sizing
    EST_WIN_RATE = 0.55
    EST_RR = 1.5               # Risk/reward ratio
    MAX_EQUITY_PCT = 0.05      # 5% of equity cap

    # Exit parameters
    TAKE_PROFIT_PCT = 0.50     # Close at 50% of premium earned
    STOP_LOSS_MULT = 2.0       # Stop at 200% of premium (loss = 2x credit)
    TIME_STOP_DTE = 3          # Close at 3 DTE
    KILL_SWITCH_LOSSES = 3     # Consecutive losses to trigger halt
    KILL_SWITCH_DAYS = 14      # Days to halt after kill switch

    def __init__(self):
        self.consecutive_losses = 0
        self.halted_until_idx = -1

    def check_entry(
        self,
        row: pd.Series,
        idx: int,
        has_active_position: bool,
    ) -> Optional[TradeSignal]:
        """Check if entry conditions are met for this day."""
        if has_active_position:
            return None
        if idx < self.halted_until_idx:
            return None

        # ---- Condition 1: CRISIS regime ----
        regime = row.get("regime_threshold")
        if pd.isna(regime) or regime != "CRISIS":
            return None

        # ---- Condition 2: VRP z-score recovering ----
        vrp_z = row.get("vrp_zscore")
        if pd.isna(vrp_z) or vrp_z <= -0.5:
            return None

        # ---- Condition 3: VoV declining ----
        vov = row.get("vov_30d")
        vov_avg = row.get("vov_30d_5d_avg")  # Pre-computed in simulation
        if pd.isna(vov) or pd.isna(vov_avg) or vov >= vov_avg:
            return None

        # ---- Condition 4: RSI in neutral zone ----
        rsi = row.get("rsi_14")
        if pd.isna(rsi) or rsi < 30 or rsi > 70:
            return None

        # ---- Build trade signal ----
        S = row["close"]
        iv = row.get("iv_synthetic_30d", 0.60)
        if pd.isna(iv) or iv <= 0:
            iv = 0.60

        # Iron condor legs
        T = self.EXPIRY_DAYS / 365.0

        # Short legs (15% OTM)
        K_short_call = S * (1 + self.SHORT_STRIKE_OTM)
        K_short_put = S * (1 - self.SHORT_STRIKE_OTM)
        # Long legs (25% OTM -- wings)
        K_long_call = S * (1 + self.LONG_STRIKE_OTM)
        K_long_put = S * (1 - self.LONG_STRIKE_OTM)

        # Premiums
        prem_short_call = bs_price(S, K_short_call, T, 0, iv, "call")
        prem_short_put = bs_price(S, K_short_put, T, 0, iv, "put")
        prem_long_call = bs_price(S, K_long_call, T, 0, iv, "call")
        prem_long_put = bs_price(S, K_long_put, T, 0, iv, "put")

        net_credit = (prem_short_call + prem_short_put) - (prem_long_call + prem_long_put)
        if net_credit <= 0:
            return None  # No credit -> skip

        # Max loss = wing width - net credit (per BTC)
        wing_width_call = K_long_call - K_short_call
        max_loss_per_btc = wing_width_call - net_credit  # Same for put side

        # Sizing via half-Kelly, capped at 5% equity
        # (equity will be set by the simulator)
        legs = [
            OptionLeg("call", "short", 1 + self.SHORT_STRIKE_OTM, self.EXPIRY_DAYS, 0.0, prem_short_call),
            OptionLeg("put", "short", 1 - self.SHORT_STRIKE_OTM, self.EXPIRY_DAYS, 0.0, prem_short_put),
            OptionLeg("call", "long", 1 + self.LONG_STRIKE_OTM, self.EXPIRY_DAYS, 0.0, prem_long_call),
            OptionLeg("put", "long", 1 - self.LONG_STRIKE_OTM, self.EXPIRY_DAYS, 0.0, prem_long_put),
        ]

        return TradeSignal(
            strategy_name=self.NAME,
            timestamp=str(row["date"]),
            legs=legs,
            max_loss_usd=max_loss_per_btc,  # Per BTC, size set later
            confidence=min(0.5 + (vrp_z + 0.5) * 0.3, 0.85),
            regime=regime,
            rationale=(
                f"CRISIS regime, VRP z={vrp_z:.2f} (recovering), "
                f"VoV declining ({vov:.3f} < {vov_avg:.3f}), RSI={rsi:.1f}"
            ),
            stop_loss_pct=self.STOP_LOSS_MULT,
            take_profit_pct=self.TAKE_PROFIT_PCT,
            time_stop_days=self.TIME_STOP_DTE,
        )

    def size_trade(
        self, signal: TradeSignal, equity: float, spot: float,
    ) -> float:
        """Determine position size in BTC.

        Returns size in BTC (each leg). Risk = max_loss * size.
        """
        hk = half_kelly_fraction(self.EST_WIN_RATE, self.EST_RR)
        max_risk = min(hk, self.MAX_EQUITY_PCT) * equity
        max_loss_per_btc = signal.max_loss_usd
        if max_loss_per_btc <= 0:
            return 0.0
        size = max_risk / max_loss_per_btc
        # Floor at a sensible minimum
        return max(size, 1e-6)

    def reprice_position(
        self,
        spot: float,
        iv: float,
        dte_days: int,
        legs: List[OptionLeg],
        entry_spot: float,
    ) -> float:
        """Reprice all legs and return current net value of the position.

        For a short iron condor, position value = credit received - current cost to close.
        We return P&L relative to entry credit.
        """
        T = max(dte_days, 0) / 365.0
        current_value = 0.0
        for leg in legs:
            K = entry_spot * leg.strike_pct
            price = bs_price(spot, K, T, 0, iv, leg.option_type)
            if leg.direction == "short":
                # Short: we received premium at entry, current liability = price
                current_value += leg.entry_premium_usd - price
            else:
                # Long: we paid premium at entry, current asset = price
                current_value += price - leg.entry_premium_usd
        return current_value  # Positive = profit

    def record_loss(self):
        """Record a loss for kill switch tracking."""
        self.consecutive_losses += 1

    def record_win(self):
        """Record a win; reset consecutive loss counter."""
        self.consecutive_losses = 0

    def check_kill_switch(self, current_idx: int):
        """Activate kill switch if consecutive losses hit threshold."""
        if self.consecutive_losses >= self.KILL_SWITCH_LOSSES:
            self.halted_until_idx = current_idx + self.KILL_SWITCH_DAYS
            self.consecutive_losses = 0  # Reset after halting


# =============================================================================
# 5. STRATEGY B - MOMENTUM BREAKOUT (Long OTM)
# =============================================================================

class StrategyB_MomentumBreakout:
    """
    Momentum Breakout via Long OTM Options.

    Thesis: Regime transitions to higher-vol states create directional
    momentum. When options are cheap (high cheap_options_score) and
    momentum confirms direction, buy OTM options for convex payoff.

    Entry Conditions (ALL must be true):
      1. cheap_options_score > 70
      2. Regime transition in last 5 days (regime_change == 1 within window)
      3. New regime is higher vol than previous
      4. Momentum confirmation:
         - Bullish: close > sma_50 AND macd_histogram > 0
         - Bearish: close < sma_50 AND macd_histogram < 0
      5. park_cc_ratio > 1.1 (intraday vol expansion)

    Trade Structure:
      - Bullish: Long 12% OTM call, 30d expiry
      - Bearish: Long 12% OTM put, 30d expiry

    Sizing:
      - Risk exactly 2% of equity (premium = max loss)
      - If cheap_options_score > 90: risk 3%

    Exit Rules:
      - Take profit: 300% (3x premium)
      - Partial exit: Close 50% at 200% (2x premium)
      - Stop loss: 80% of premium lost (value drops to 20% of entry)
      - Time stop: DTE <= 15
      - Trailing stop: When up 150%+, trail at 50% of max gain
      - Kill switch: 5 consecutive losses -> halve size for 10 trades
    """

    NAME = "B_Momentum_LongOTM"

    # Trade parameters
    OTM_STRIKE = 0.12          # 12% OTM
    EXPIRY_DAYS = 30

    # Sizing
    BASE_RISK_PCT = 0.02       # 2% of equity
    HIGH_CONF_RISK_PCT = 0.03  # 3% when cheap_options_score > 90
    CHEAP_THRESHOLD = 70
    HIGH_CONF_THRESHOLD = 90

    # Exit parameters
    TAKE_PROFIT_MULT = 3.0     # 300% of premium
    PARTIAL_EXIT_MULT = 2.0    # Close 50% at 200%
    PARTIAL_EXIT_FRAC = 0.5    # Fraction to close at partial
    STOP_LOSS_FLOOR = 0.20     # Exit when value drops to 20% of entry premium
    TIME_STOP_DTE = 15
    TRAILING_ACTIVATION = 1.5  # Activate trailing stop at 150% gain
    TRAILING_RETRACEMENT = 0.50  # Trail at 50% of max gain

    # Kill switch
    KILL_SWITCH_LOSSES = 5
    KILL_SWITCH_TRADES = 10    # Number of trades at half-size

    # Regime transition lookback
    REGIME_CHANGE_LOOKBACK = 5

    def __init__(self):
        self.consecutive_losses = 0
        self.half_size_remaining = 0  # Trades left at half size

    def check_entry(
        self,
        row: pd.Series,
        idx: int,
        df: pd.DataFrame,
        has_active_position: bool,
    ) -> Optional[TradeSignal]:
        """Check if entry conditions are met for this day."""
        if has_active_position:
            return None

        # ---- Condition 1: Cheap options ----
        score = row.get("cheap_options_score")
        if pd.isna(score) or score <= self.CHEAP_THRESHOLD:
            return None

        # ---- Condition 2 & 3: Regime transition to higher vol ----
        regime_str = row.get("regime_threshold")
        if pd.isna(regime_str):
            return None

        # Check for regime change in last 5 days
        lookback_start = max(0, idx - self.REGIME_CHANGE_LOOKBACK + 1)
        recent = df.iloc[lookback_start : idx + 1]
        if recent["regime_change"].sum() == 0:
            return None

        # Find the most recent regime change
        change_rows = recent[recent["regime_change"] == 1]
        if len(change_rows) == 0:
            return None
        last_change = change_rows.iloc[-1]
        prev_regime_str = last_change.get("prev_regime")
        new_regime_str = last_change.get("regime_threshold")

        if pd.isna(prev_regime_str) or pd.isna(new_regime_str):
            return None

        try:
            prev_regime = VolRegime(prev_regime_str)
            new_regime = VolRegime(new_regime_str)
        except ValueError:
            return None

        if new_regime <= prev_regime:
            return None  # Must be transitioning to HIGHER vol

        # ---- Condition 4: Momentum confirmation ----
        close = row["close"]
        sma_50 = row.get("sma_50")
        macd_hist = row.get("macd_histogram")

        if pd.isna(sma_50) or pd.isna(macd_hist):
            return None

        if close > sma_50 and macd_hist > 0:
            direction = "bullish"
        elif close < sma_50 and macd_hist < 0:
            direction = "bearish"
        else:
            return None  # No clear momentum

        # ---- Condition 5: Intraday vol expansion ----
        park_cc = row.get("park_cc_ratio")
        if pd.isna(park_cc) or park_cc <= 1.1:
            return None

        # ---- Build trade signal ----
        S = close
        iv = row.get("iv_synthetic_30d", 0.60)
        if pd.isna(iv) or iv <= 0:
            iv = 0.60

        T = self.EXPIRY_DAYS / 365.0

        if direction == "bullish":
            K = S * (1 + self.OTM_STRIKE)
            opt_type = "call"
            strike_pct = 1 + self.OTM_STRIKE
        else:
            K = S * (1 - self.OTM_STRIKE)
            opt_type = "put"
            strike_pct = 1 - self.OTM_STRIKE

        premium = bs_price(S, K, T, 0, iv, opt_type)
        if premium <= 0:
            return None

        leg = OptionLeg(
            option_type=opt_type,
            direction="long",
            strike_pct=strike_pct,
            expiry_days=self.EXPIRY_DAYS,
            size_btc=0.0,  # Set by size_trade
            entry_premium_usd=premium,
        )

        return TradeSignal(
            strategy_name=self.NAME,
            timestamp=str(row["date"]),
            legs=[leg],
            max_loss_usd=premium,  # Premium = max loss for long option
            confidence=min(score / 100.0, 0.95),
            regime=regime_str,
            rationale=(
                f"{direction.upper()} breakout: {prev_regime_str}->{new_regime_str}, "
                f"score={score:.1f}, park_cc={park_cc:.2f}, MACD_hist={macd_hist:.1f}"
            ),
            stop_loss_pct=self.STOP_LOSS_FLOOR,
            take_profit_pct=self.TAKE_PROFIT_MULT,
            time_stop_days=self.TIME_STOP_DTE,
        )

    def size_trade(
        self, signal: TradeSignal, equity: float, spot: float,
    ) -> float:
        """Determine position size in BTC.

        Risk = premium per BTC * size. We risk 2% (or 3%) of equity.
        """
        score = signal.confidence * 100  # Recover score from confidence
        risk_pct = self.HIGH_CONF_RISK_PCT if score > self.HIGH_CONF_THRESHOLD else self.BASE_RISK_PCT

        # Kill switch: halve size
        if self.half_size_remaining > 0:
            risk_pct /= 2.0
            self.half_size_remaining -= 1

        risk_budget = risk_pct * equity
        premium_per_btc = signal.max_loss_usd  # For long options, premium = max loss
        if premium_per_btc <= 0:
            return 0.0
        return risk_budget / premium_per_btc

    def record_loss(self):
        self.consecutive_losses += 1
        if self.consecutive_losses >= self.KILL_SWITCH_LOSSES:
            self.half_size_remaining = self.KILL_SWITCH_TRADES
            self.consecutive_losses = 0

    def record_win(self):
        self.consecutive_losses = 0


# =============================================================================
# 6. BACKTESTING ENGINE
# =============================================================================

class BacktestEngine:
    """Event-driven backtester for option strategies.

    Walks through the dataset day-by-day, checks entry conditions,
    simulates option repricing, and enforces exit rules.
    """

    def __init__(self, df: pd.DataFrame, initial_equity: float = 100.0):
        self.df = df.copy()
        self.initial_equity = initial_equity

        # Pre-compute vov_30d 5-day average for Strategy A
        self.df["vov_30d_5d_avg"] = self.df["vov_30d"].rolling(5).mean()

    def run_strategy_a(self) -> Tuple[List[TradeResult], pd.Series]:
        """Backtest Strategy A: Iron Condor Vol Selling."""
        strat = StrategyA_VolSelling()
        results: List[TradeResult] = []
        equity = self.initial_equity
        equity_curve = pd.Series(index=self.df.index, dtype=float)

        # Active position tracking
        active = False
        entry_idx = 0
        entry_spot = 0.0
        entry_credit = 0.0
        entry_legs: List[OptionLeg] = []
        position_size = 0.0
        entry_date = ""
        entry_regime = ""

        for idx in range(len(self.df)):
            row = self.df.iloc[idx]
            equity_curve.iloc[idx] = equity

            if active:
                # ---- Reprice and check exits ----
                days_held = idx - entry_idx
                dte = strat.EXPIRY_DAYS - days_held
                spot = row["close"]
                iv = row.get("iv_synthetic_30d", 0.60)
                if pd.isna(iv) or iv <= 0:
                    iv = 0.60

                pnl_per_btc = strat.reprice_position(
                    spot, iv, dte, entry_legs, entry_spot
                )
                total_pnl = pnl_per_btc * position_size

                # Exit conditions
                exit_reason = None

                # Take profit: P&L > 50% of credit received
                if pnl_per_btc >= entry_credit * strat.TAKE_PROFIT_PCT:
                    exit_reason = "take_profit"

                # Stop loss: P&L < -(2x credit)
                elif pnl_per_btc <= -entry_credit * strat.STOP_LOSS_MULT:
                    exit_reason = "stop_loss"

                # Time stop
                elif dte <= strat.TIME_STOP_DTE:
                    exit_reason = "time_stop"

                # Regime exit: regime changed from CRISIS
                current_regime = row.get("regime_threshold")
                if current_regime != "CRISIS" and not pd.isna(current_regime):
                    exit_reason = "regime_exit"

                # Expiry
                if dte <= 0:
                    exit_reason = "expiry"

                if exit_reason:
                    equity += total_pnl
                    pnl_pct = total_pnl / max(equity - total_pnl, 1e-6)

                    results.append(TradeResult(
                        strategy_name=strat.NAME,
                        entry_date=entry_date,
                        exit_date=str(row["date"]),
                        entry_price_btc=entry_spot,
                        exit_price_btc=spot,
                        pnl_usd=total_pnl,
                        pnl_pct=pnl_pct,
                        holding_days=days_held,
                        exit_reason=exit_reason,
                        regime_at_entry=entry_regime,
                        equity_before=equity - total_pnl,
                        equity_after=equity,
                    ))

                    if total_pnl < 0:
                        strat.record_loss()
                        strat.check_kill_switch(idx)
                    else:
                        strat.record_win()

                    active = False
                    equity_curve.iloc[idx] = equity

            if not active:
                # ---- Check entry ----
                signal = strat.check_entry(row, idx, has_active_position=False)
                if signal is not None:
                    position_size = strat.size_trade(signal, equity, row["close"])
                    if position_size > 0:
                        active = True
                        entry_idx = idx
                        entry_spot = row["close"]
                        entry_legs = signal.legs
                        # Set actual sizes
                        for leg in entry_legs:
                            leg.size_btc = position_size
                        # Net credit per BTC
                        entry_credit = sum(
                            leg.entry_premium_usd for leg in entry_legs
                            if leg.direction == "short"
                        ) - sum(
                            leg.entry_premium_usd for leg in entry_legs
                            if leg.direction == "long"
                        )
                        entry_date = str(row["date"])
                        entry_regime = str(row.get("regime_threshold", ""))

        # Close any open position at end
        if active:
            row = self.df.iloc[-1]
            spot = row["close"]
            iv = row.get("iv_synthetic_30d", 0.60)
            days_held = len(self.df) - 1 - entry_idx
            dte = max(strat.EXPIRY_DAYS - days_held, 0)
            pnl_per_btc = strat.reprice_position(spot, iv, dte, entry_legs, entry_spot)
            total_pnl = pnl_per_btc * position_size
            equity += total_pnl
            results.append(TradeResult(
                strategy_name=strat.NAME,
                entry_date=entry_date,
                exit_date=str(row["date"]),
                entry_price_btc=entry_spot,
                exit_price_btc=spot,
                pnl_usd=total_pnl,
                pnl_pct=total_pnl / max(equity - total_pnl, 1e-6),
                holding_days=days_held,
                exit_reason="end_of_data",
                regime_at_entry=entry_regime,
                equity_before=equity - total_pnl,
                equity_after=equity,
            ))

        return results, equity_curve

    def run_strategy_b(self) -> Tuple[List[TradeResult], pd.Series]:
        """Backtest Strategy B: Momentum Breakout Long OTM."""
        strat = StrategyB_MomentumBreakout()
        results: List[TradeResult] = []
        equity = self.initial_equity
        equity_curve = pd.Series(index=self.df.index, dtype=float)

        # Active position tracking
        active = False
        entry_idx = 0
        entry_spot = 0.0
        entry_premium = 0.0  # Premium paid per BTC
        entry_leg: Optional[OptionLeg] = None
        position_size = 0.0
        original_size = 0.0
        entry_date = ""
        entry_regime = ""
        max_value_ratio = 1.0  # Peak value / entry premium
        partial_taken = False

        for idx in range(len(self.df)):
            row = self.df.iloc[idx]
            equity_curve.iloc[idx] = equity

            if active:
                # ---- Reprice and check exits ----
                days_held = idx - entry_idx
                dte = strat.EXPIRY_DAYS - days_held
                spot = row["close"]
                iv = row.get("iv_synthetic_30d", 0.60)
                if pd.isna(iv) or iv <= 0:
                    iv = 0.60

                T = max(dte, 0) / 365.0
                K = entry_spot * entry_leg.strike_pct
                current_price = bs_price(spot, K, T, 0, iv, entry_leg.option_type)
                value_ratio = current_price / max(entry_premium, 1e-10)

                # Track peak for trailing stop
                max_value_ratio = max(max_value_ratio, value_ratio)

                # P&L per BTC
                pnl_per_btc = current_price - entry_premium
                total_pnl = pnl_per_btc * position_size

                exit_reason = None
                partial_close = False

                # ---- Partial exit at 200% ----
                if not partial_taken and value_ratio >= strat.PARTIAL_EXIT_MULT:
                    partial_close = True
                    partial_taken = True

                # ---- Take profit at 300% ----
                if value_ratio >= strat.TAKE_PROFIT_MULT:
                    exit_reason = "take_profit"

                # ---- Stop loss: value dropped to 20% of entry ----
                elif value_ratio <= strat.STOP_LOSS_FLOOR:
                    exit_reason = "stop_loss"

                # ---- Time stop ----
                elif dte <= strat.TIME_STOP_DTE:
                    exit_reason = "time_stop"

                # ---- Trailing stop ----
                elif max_value_ratio >= (1 + strat.TRAILING_ACTIVATION):
                    # Trailing stop: if value dropped 50% from peak gain
                    peak_gain = max_value_ratio - 1.0
                    current_gain = value_ratio - 1.0
                    if current_gain <= peak_gain * strat.TRAILING_RETRACEMENT:
                        exit_reason = "trailing_stop"

                # ---- Expiry ----
                if dte <= 0:
                    exit_reason = "expiry"

                # Handle partial exit
                if partial_close and exit_reason is None:
                    close_size = original_size * strat.PARTIAL_EXIT_FRAC
                    partial_pnl = pnl_per_btc * close_size
                    equity += partial_pnl
                    position_size -= close_size

                    results.append(TradeResult(
                        strategy_name=strat.NAME,
                        entry_date=entry_date,
                        exit_date=str(row["date"]),
                        entry_price_btc=entry_spot,
                        exit_price_btc=spot,
                        pnl_usd=partial_pnl,
                        pnl_pct=partial_pnl / max(equity - partial_pnl, 1e-6),
                        holding_days=days_held,
                        exit_reason="partial_exit_200pct",
                        regime_at_entry=entry_regime,
                        equity_before=equity - partial_pnl,
                        equity_after=equity,
                        max_premium_value=max_value_ratio,
                    ))

                if exit_reason:
                    total_pnl = pnl_per_btc * position_size
                    equity += total_pnl
                    pnl_pct = total_pnl / max(equity - total_pnl, 1e-6)

                    results.append(TradeResult(
                        strategy_name=strat.NAME,
                        entry_date=entry_date,
                        exit_date=str(row["date"]),
                        entry_price_btc=entry_spot,
                        exit_price_btc=spot,
                        pnl_usd=total_pnl,
                        pnl_pct=pnl_pct,
                        holding_days=days_held,
                        exit_reason=exit_reason,
                        regime_at_entry=entry_regime,
                        equity_before=equity - total_pnl,
                        equity_after=equity,
                        max_premium_value=max_value_ratio,
                    ))

                    if total_pnl < 0 and not partial_taken:
                        # Only count as full loss if no partial was taken
                        strat.record_loss()
                    elif total_pnl < 0 and partial_taken:
                        # Partial profit + final loss -> net depends
                        strat.record_win()  # Had partial profit
                    else:
                        strat.record_win()

                    active = False
                    equity_curve.iloc[idx] = equity

            if not active:
                # ---- Check entry ----
                signal = strat.check_entry(row, idx, self.df, has_active_position=False)
                if signal is not None:
                    position_size = strat.size_trade(signal, equity, row["close"])
                    if position_size > 0:
                        active = True
                        entry_idx = idx
                        entry_spot = row["close"]
                        entry_leg = signal.legs[0]
                        entry_leg.size_btc = position_size
                        entry_premium = entry_leg.entry_premium_usd
                        original_size = position_size
                        entry_date = str(row["date"])
                        entry_regime = str(row.get("regime_threshold", ""))
                        max_value_ratio = 1.0
                        partial_taken = False

        # Close any open position at end
        if active:
            row = self.df.iloc[-1]
            spot = row["close"]
            iv = row.get("iv_synthetic_30d", 0.60)
            days_held = len(self.df) - 1 - entry_idx
            dte = max(strat.EXPIRY_DAYS - days_held, 0)
            T = dte / 365.0
            K = entry_spot * entry_leg.strike_pct
            current_price = bs_price(spot, K, T, 0, iv, entry_leg.option_type)
            pnl_per_btc = current_price - entry_premium
            total_pnl = pnl_per_btc * position_size
            equity += total_pnl
            results.append(TradeResult(
                strategy_name=strat.NAME,
                entry_date=entry_date,
                exit_date=str(row["date"]),
                entry_price_btc=entry_spot,
                exit_price_btc=spot,
                pnl_usd=total_pnl,
                pnl_pct=total_pnl / max(equity - total_pnl, 1e-6),
                holding_days=days_held,
                exit_reason="end_of_data",
                regime_at_entry=entry_regime,
                equity_before=equity - total_pnl,
                equity_after=equity,
                max_premium_value=max_value_ratio,
            ))

        return results, equity_curve


# =============================================================================
# 7. REPORTING
# =============================================================================

def compute_stats(results: List[TradeResult], initial_equity: float) -> Dict:
    """Compute summary statistics from trade results."""
    if not results:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "avg_win": 0.0,
            "avg_loss": 0.0, "profit_factor": 0.0, "max_drawdown_pct": 0.0,
            "final_equity": initial_equity, "total_return_pct": 0.0,
            "avg_holding_days": 0.0, "sharpe": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0,
        }

    wins = [r for r in results if r.pnl_usd > 0]
    losses = [r for r in results if r.pnl_usd <= 0]
    pnls = [r.pnl_usd for r in results]

    total_trades = len(results)
    win_rate = len(wins) / total_trades if total_trades > 0 else 0
    avg_win = np.mean([r.pnl_usd for r in wins]) if wins else 0.0
    avg_loss = np.mean([abs(r.pnl_usd) for r in losses]) if losses else 0.0
    gross_profit = sum(r.pnl_usd for r in wins) if wins else 0.0
    gross_loss = sum(abs(r.pnl_usd) for r in losses) if losses else 1e-10
    profit_factor = gross_profit / gross_loss

    # Equity curve from results
    equity_series = [initial_equity]
    for r in results:
        equity_series.append(r.equity_after)
    eq = np.array(equity_series)
    running_max = np.maximum.accumulate(eq)
    drawdowns = (eq - running_max) / running_max
    max_drawdown = abs(drawdowns.min())

    final_equity = results[-1].equity_after
    total_return = (final_equity - initial_equity) / initial_equity * 100
    avg_holding = np.mean([r.holding_days for r in results])

    # Trade-level Sharpe (annualised)
    pnl_arr = np.array(pnls)
    if pnl_arr.std() > 0:
        sharpe = (pnl_arr.mean() / pnl_arr.std()) * np.sqrt(252 / max(avg_holding, 1))
    else:
        sharpe = 0.0

    return {
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown * 100,
        "final_equity": final_equity,
        "total_return_pct": total_return,
        "avg_holding_days": avg_holding,
        "sharpe": sharpe,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }


def print_report(name: str, stats: Dict, results: List[TradeResult]):
    """Print formatted strategy performance report."""
    print(f"\n{'=' * 70}")
    print(f"  {name}")
    print(f"{'=' * 70}")
    print(f"  Total Trades:     {stats['total_trades']}")
    print(f"  Wins / Losses:    {stats['wins']} / {stats['losses']}")
    print(f"  Win Rate:         {stats['win_rate']:.1%}")
    print(f"  Avg Win:          ${stats['avg_win']:.4f}")
    print(f"  Avg Loss:         ${stats['avg_loss']:.4f}")
    print(f"  Profit Factor:    {stats['profit_factor']:.2f}")
    print(f"  Gross Profit:     ${stats['gross_profit']:.4f}")
    print(f"  Gross Loss:       ${stats['gross_loss']:.4f}")
    print(f"  Sharpe (ann.):    {stats['sharpe']:.2f}")
    print(f"  Avg Hold Days:    {stats['avg_holding_days']:.1f}")
    print(f"  Max Drawdown:     {stats['max_drawdown_pct']:.2f}%")
    print(f"  Final Equity:     ${stats['final_equity']:.4f}")
    print(f"  Total Return:     {stats['total_return_pct']:.2f}%")
    print(f"{'=' * 70}")

    if results:
        # Exit reason breakdown
        from collections import Counter
        reasons = Counter(r.exit_reason for r in results)
        print(f"\n  Exit Reasons:")
        for reason, count in reasons.most_common():
            print(f"    {reason:25s} {count:4d} ({count/len(results)*100:.1f}%)")

        # Show sample trades
        print(f"\n  Sample Trades (first 10):")
        print(f"  {'Entry':>12s} {'Exit':>12s} {'PnL$':>10s} {'PnL%':>8s} {'Days':>5s} {'Reason':>20s}")
        for r in results[:10]:
            print(
                f"  {r.entry_date:>12s} {r.exit_date:>12s} "
                f"{r.pnl_usd:>10.4f} {r.pnl_pct:>7.2%} "
                f"{r.holding_days:>5d} {r.exit_reason:>20s}"
            )
# =============================================================================
# 9. STRATEGY C - EVENT VOL EXPLOITATION
# =============================================================================

class StrategyC_EventVol:
    """
    Event Volatility Exploitation Strategy.

    Thesis: Known events (halvings, quarterly expiries, historical crash
    anniversaries) create predictable vol patterns. IV tends to inflate
    before events (sell straddle pre-event) and crush after events
    (buy straddle post-event when vol is depressed).

    Sub-strategies:
      C1 - Pre-Event Vol Sell: Sell ATM straddle 7-21 days before event
           when IV > 1.15x RV. 14d expiry. TP 60%, SL 40%.
      C2 - Post-Event Vol Buy: Buy ATM straddle within 7d after event
           when vol has crushed. 30d expiry. TP 200%, SL 70%.
    """

    NAME = "C_EventVol"

    # Hardcoded event dates
    HALVING_DATES = ["2016-07-09", "2020-05-11", "2024-04-19"]
    CRASH_DATES = ["2017-12-17", "2020-03-12", "2021-05-19", "2022-05-09", "2022-11-08"]

    # Pre-event parameters
    PRE_EVENT_WINDOW_MIN = 7    # Enter 7-21 days before event
    PRE_EVENT_WINDOW_MAX = 21
    PRE_IV_RV_RATIO = 1.15      # IV must exceed 1.15x RV
    PRE_EXPIRY_DAYS = 14
    PRE_RISK_PCT = 0.03         # 3% equity risk
    PRE_TP_PCT = 0.60           # Take profit at 60% of premium
    PRE_SL_PCT = 0.40           # Stop loss at 40% loss (position value = 1.4x entry)

    # Post-event parameters
    POST_EVENT_WINDOW = 7       # Enter within 7 days after event
    POST_EXPIRY_DAYS = 30
    POST_RISK_PCT = 0.02        # 2% equity risk
    POST_TP_MULT = 2.0          # Take profit at 200% (3x entry premium)
    POST_SL_FLOOR = 0.30        # Stop loss when value drops to 30% of entry

    # Kill switch
    KILL_SWITCH_LOSSES = 3
    KILL_SWITCH_DAYS = 30

    def __init__(self):
        self.consecutive_losses = 0
        self.halted_until_idx = -1
        self._quarterly_expiries = None
        self._all_events = None

    def _generate_quarterly_expiries(self, start_year=2014, end_year=2027):
        """Generate last Friday of Mar, Jun, Sep, Dec for each year."""
        from datetime import date, timedelta
        expiries = []
        for year in range(start_year, end_year + 1):
            for month in [3, 6, 9, 12]:
                # Find last day of month
                if month == 12:
                    last_day = date(year, 12, 31)
                else:
                    last_day = date(year, month + 1, 1) - timedelta(days=1)
                # Walk back to Friday (weekday 4)
                while last_day.weekday() != 4:
                    last_day -= timedelta(days=1)
                expiries.append(last_day.strftime("%Y-%m-%d"))
        return expiries

    def _get_all_events(self):
        """Build combined event list with types."""
        if self._all_events is not None:
            return self._all_events

        self._quarterly_expiries = self._generate_quarterly_expiries()
        events = []
        for d in self.HALVING_DATES:
            events.append((d, "halving"))
        for d in self.CRASH_DATES:
            events.append((d, "crash"))
        for d in self._quarterly_expiries:
            events.append((d, "quarterly_expiry"))
        self._all_events = events
        return events

    def _days_to_nearest_event(self, date_str):
        """Return (days_until, days_since, event_type) for nearest event."""
        from datetime import datetime
        current = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        events = self._get_all_events()

        best_until = (9999, "none")
        best_since = (9999, "none")

        for evt_date_str, evt_type in events:
            evt = datetime.strptime(evt_date_str, "%Y-%m-%d")
            delta = (evt - current).days
            if delta > 0 and delta < best_until[0]:
                best_until = (delta, evt_type)
            elif delta <= 0 and abs(delta) < best_since[0]:
                best_since = (abs(delta), evt_type)

        return best_until, best_since  # (days_until, type), (days_since, type)

    def check_entry(
        self,
        row: pd.Series,
        idx: int,
        has_active_position: bool,
    ) -> Optional[TradeSignal]:
        """Check entry conditions for event vol strategy."""
        if has_active_position:
            return None
        if idx < self.halted_until_idx:
            return None

        date_str = str(row["date"])[:10]
        regime = row.get("regime_threshold")
        if pd.isna(regime):
            return None

        iv = row.get("iv_synthetic_30d")
        rv = row.get("rv_cc_30d")
        if pd.isna(iv) or pd.isna(rv) or iv <= 0 or rv <= 0:
            return None

        S = row["close"]
        (days_until, evt_type_until), (days_since, evt_type_since) = self._days_to_nearest_event(date_str)

        # === C1: Pre-Event Vol Sell ===
        if (self.PRE_EVENT_WINDOW_MIN <= days_until <= self.PRE_EVENT_WINDOW_MAX
                and iv > rv * self.PRE_IV_RV_RATIO
                and regime in ("MEDIUM", "HIGH")):

            T = self.PRE_EXPIRY_DAYS / 365.0
            K = S  # ATM
            prem_call = bs_price(S, K, T, 0, iv, "call")
            prem_put = bs_price(S, K, T, 0, iv, "put")
            net_credit = prem_call + prem_put

            if net_credit <= 0:
                return None

            legs = [
                OptionLeg("call", "short", 1.0, self.PRE_EXPIRY_DAYS, 0.0, prem_call),
                OptionLeg("put", "short", 1.0, self.PRE_EXPIRY_DAYS, 0.0, prem_put),
            ]

            return TradeSignal(
                strategy_name=f"{self.NAME}_PreEvent",
                timestamp=date_str,
                legs=legs,
                max_loss_usd=net_credit * 2.0,  # Straddle max loss is theoretically unlimited, cap at 2x credit for sizing
                confidence=min(0.5 + (iv / rv - 1.15) * 2.0, 0.85),
                regime=regime,
                rationale=(
                    f"Pre-event sell straddle: {evt_type_until} in {days_until}d, "
                    f"IV/RV={iv/rv:.2f}, regime={regime}"
                ),
                stop_loss_pct=self.PRE_SL_PCT,
                take_profit_pct=self.PRE_TP_PCT,
                time_stop_days=2,
            )

        # === C2: Post-Event Vol Buy ===
        if (0 <= days_since <= self.POST_EVENT_WINDOW
                and regime in ("LOW", "MEDIUM")):

            T = self.POST_EXPIRY_DAYS / 365.0
            K = S  # ATM
            prem_call = bs_price(S, K, T, 0, iv, "call")
            prem_put = bs_price(S, K, T, 0, iv, "put")
            total_premium = prem_call + prem_put

            if total_premium <= 0:
                return None

            legs = [
                OptionLeg("call", "long", 1.0, self.POST_EXPIRY_DAYS, 0.0, prem_call),
                OptionLeg("put", "long", 1.0, self.POST_EXPIRY_DAYS, 0.0, prem_put),
            ]

            return TradeSignal(
                strategy_name=f"{self.NAME}_PostEvent",
                timestamp=date_str,
                legs=legs,
                max_loss_usd=total_premium,  # Long straddle max loss = premium paid
                confidence=0.55,
                regime=regime,
                rationale=(
                    f"Post-event buy straddle: {evt_type_since} was {days_since}d ago, "
                    f"vol may expand, regime={regime}"
                ),
                stop_loss_pct=self.POST_SL_FLOOR,
                take_profit_pct=self.POST_TP_MULT,
                time_stop_days=20,
            )

        return None

    def size_trade(self, signal: TradeSignal, equity: float, spot: float) -> float:
        """Size based on sub-strategy type."""
        if "PreEvent" in signal.strategy_name:
            risk_pct = self.PRE_RISK_PCT
        else:
            risk_pct = self.POST_RISK_PCT

        risk_budget = risk_pct * equity
        max_loss_per_btc = signal.max_loss_usd
        if max_loss_per_btc <= 0:
            return 0.0
        return risk_budget / max_loss_per_btc

    def reprice_position(self, spot, iv, dte_days, legs, entry_spot, is_short_straddle):
        """Reprice straddle position and return P&L per BTC."""
        T = max(dte_days, 0) / 365.0
        pnl = 0.0
        for leg in legs:
            K = entry_spot * leg.strike_pct  # ATM so strike_pct = 1.0
            current_price = bs_price(spot, K, T, 0, iv, leg.option_type)
            if leg.direction == "short":
                pnl += leg.entry_premium_usd - current_price
            else:
                pnl += current_price - leg.entry_premium_usd
        return pnl

    def record_loss(self):
        self.consecutive_losses += 1

    def record_win(self):
        self.consecutive_losses = 0

    def check_kill_switch(self, current_idx):
        if self.consecutive_losses >= self.KILL_SWITCH_LOSSES:
            self.halted_until_idx = current_idx + self.KILL_SWITCH_DAYS
            self.consecutive_losses = 0


# =============================================================================
# 10. STRATEGY D - MEAN REVERSION VOL FADE
# =============================================================================

class StrategyD_MeanReversion:
    """
    Mean Reversion Volatility Fade Strategy.

    Thesis: Extreme vol readings (very high or very low percentile)
    tend to mean-revert. When vol is extremely high AND declining in
    CRISIS, sell straddles. When vol is extremely low AND regime has
    been stable LOW for 30+ days, buy straddles for the eventual
    vol explosion.

    Sub-strategies:
      D1 - Short Vol (Mean Reversion Down): Sell ATM straddle when
           rv_cc_30d percentile > 90%, vol is declining, regime CRISIS
           for 5+ days. 14d expiry. 2% risk. TP 80%, SL 150%.
      D2 - Long Vol (Mean Reversion Up): Buy ATM straddle when
           rv_cc_30d percentile < 10%, regime LOW for 30+ days.
           45d expiry. 2% risk. TP 250%, SL 70%.
    """

    NAME = "D_MeanReversion"

    # D1 - Short vol parameters
    SHORT_RV_PERCENTILE_MIN = 90    # 90th percentile
    SHORT_CRISIS_MIN_DAYS = 5
    SHORT_EXPIRY_DAYS = 14
    SHORT_RISK_PCT = 0.02
    SHORT_TP_PCT = 0.80             # 80% of premium
    SHORT_SL_PCT = 1.50             # 150% loss (position value = 2.5x entry)

    # D2 - Long vol parameters
    LONG_RV_PERCENTILE_MAX = 10     # 10th percentile
    LONG_LOW_MIN_DAYS = 30
    LONG_EXPIRY_DAYS = 45
    LONG_RISK_PCT = 0.02
    LONG_TP_MULT = 2.50             # 250% gain (3.5x entry premium)
    LONG_SL_FLOOR = 0.30            # Exit when value drops to 30% of entry

    # Kill switch
    KILL_SWITCH_LOSSES = 3
    KILL_SWITCH_DAYS = 21

    def __init__(self):
        self.consecutive_losses = 0
        self.halted_until_idx = -1

    def check_entry(
        self,
        row: pd.Series,
        idx: int,
        has_active_position: bool,
    ) -> Optional[TradeSignal]:
        """Check entry conditions."""
        if has_active_position:
            return None
        if idx < self.halted_until_idx:
            return None

        regime = row.get("regime_threshold")
        if pd.isna(regime):
            return None

        rv_pctl = row.get("rv_cc_30d_percentile")  # Pre-computed in allocator
        rv_30d = row.get("rv_cc_30d")
        rv_30d_prev = row.get("rv_cc_30d_prev")    # Pre-computed
        days_in_regime = row.get("days_since_regime_change")
        iv = row.get("iv_synthetic_30d")

        if any(pd.isna(x) for x in [rv_pctl, rv_30d, iv, days_in_regime]):
            return None
        if iv <= 0:
            return None

        S = row["close"]

        # === D1: Short Vol (Sell straddle in extreme high vol) ===
        if (rv_pctl > self.SHORT_RV_PERCENTILE_MIN
                and regime == "CRISIS"
                and days_in_regime >= self.SHORT_CRISIS_MIN_DAYS
                and not pd.isna(rv_30d_prev)
                and rv_30d < rv_30d_prev):  # Vol is declining

            T = self.SHORT_EXPIRY_DAYS / 365.0
            K = S  # ATM
            prem_call = bs_price(S, K, T, 0, iv, "call")
            prem_put = bs_price(S, K, T, 0, iv, "put")
            net_credit = prem_call + prem_put

            if net_credit <= 0:
                return None

            legs = [
                OptionLeg("call", "short", 1.0, self.SHORT_EXPIRY_DAYS, 0.0, prem_call),
                OptionLeg("put", "short", 1.0, self.SHORT_EXPIRY_DAYS, 0.0, prem_put),
            ]

            return TradeSignal(
                strategy_name=f"{self.NAME}_ShortVol",
                timestamp=str(row["date"])[:10],
                legs=legs,
                max_loss_usd=net_credit * 2.5,  # Capped for sizing (straddle theoretically unlimited)
                confidence=min(0.5 + (rv_pctl - 90) / 20, 0.80),
                regime=regime,
                rationale=(
                    f"Short vol: rv_pctl={rv_pctl:.1f}%, CRISIS {days_in_regime:.0f}d, "
                    f"vol declining ({rv_30d:.1%} < {rv_30d_prev:.1%})"
                ),
                stop_loss_pct=self.SHORT_SL_PCT,
                take_profit_pct=self.SHORT_TP_PCT,
                time_stop_days=2,
            )

        # === D2: Long Vol (Buy straddle in extreme low vol) ===
        if (rv_pctl < self.LONG_RV_PERCENTILE_MAX
                and regime == "LOW"
                and days_in_regime >= self.LONG_LOW_MIN_DAYS):

            T = self.LONG_EXPIRY_DAYS / 365.0
            K = S  # ATM
            prem_call = bs_price(S, K, T, 0, iv, "call")
            prem_put = bs_price(S, K, T, 0, iv, "put")
            total_premium = prem_call + prem_put

            if total_premium <= 0:
                return None

            legs = [
                OptionLeg("call", "long", 1.0, self.LONG_EXPIRY_DAYS, 0.0, prem_call),
                OptionLeg("put", "long", 1.0, self.LONG_EXPIRY_DAYS, 0.0, prem_put),
            ]

            return TradeSignal(
                strategy_name=f"{self.NAME}_LongVol",
                timestamp=str(row["date"])[:10],
                legs=legs,
                max_loss_usd=total_premium,
                confidence=0.55,
                regime=regime,
                rationale=(
                    f"Long vol: rv_pctl={rv_pctl:.1f}%, LOW regime {days_in_regime:.0f}d, "
                    f"vol suppressed -> mean reversion expected"
                ),
                stop_loss_pct=self.LONG_SL_FLOOR,
                take_profit_pct=self.LONG_TP_MULT,
                time_stop_days=35,
            )

        return None

    def size_trade(self, signal: TradeSignal, equity: float, spot: float) -> float:
        """Size based on sub-strategy type."""
        if "ShortVol" in signal.strategy_name:
            risk_pct = self.SHORT_RISK_PCT
        else:
            risk_pct = self.LONG_RISK_PCT

        risk_budget = risk_pct * equity
        max_loss_per_btc = signal.max_loss_usd
        if max_loss_per_btc <= 0:
            return 0.0
        return risk_budget / max_loss_per_btc

    def reprice_position(self, spot, iv, dte_days, legs, entry_spot, is_short):
        """Reprice straddle position and return P&L per BTC."""
        T = max(dte_days, 0) / 365.0
        pnl = 0.0
        for leg in legs:
            K = entry_spot * leg.strike_pct
            current_price = bs_price(spot, K, T, 0, iv, leg.option_type)
            if leg.direction == "short":
                pnl += leg.entry_premium_usd - current_price
            else:
                pnl += current_price - leg.entry_premium_usd
        return pnl

    def record_loss(self):
        self.consecutive_losses += 1

    def record_win(self):
        self.consecutive_losses = 0

    def check_kill_switch(self, current_idx):
        if self.consecutive_losses >= self.KILL_SWITCH_LOSSES:
            self.halted_until_idx = current_idx + self.KILL_SWITCH_DAYS
            self.consecutive_losses = 0


# =============================================================================
# 10.5 FRICTION MODEL - REALISTIC DERIBIT TRADING COSTS
# =============================================================================

class FrictionModel:
    """Realistic market friction model for Deribit BTC options.

    Fee structure (Deribit actual):
      - Maker fee: 0.03% of underlying notional
      - Taker fee: 0.05% of underlying notional
      - Delivery fee: 0.03%
      - Fee cap: 12.5% of option price (important for cheap OTM options)

    Bid-ask spread model (empirical from Deribit order books):
      - ATM: ~1% of option price
      - 10% OTM: ~2%
      - 20% OTM: ~3.5%
      - 30%+ OTM: ~5%
      Formula: spread_pct = 0.01 + 0.04 * abs(moneyness - 1), capped at 0.05
      In low-liquidity regimes (vol > 100%), multiply spread by 1.5x

    Slippage model:
      - Base: 0.1% of notional
      - Size-scaled: +0.05% per $1,000 notional above $5,000
      - Cap: 0.5%
    """

    # Deribit fee schedule
    MAKER_FEE_PCT = 0.0003       # 0.03% of underlying
    TAKER_FEE_PCT = 0.0005       # 0.05% of underlying
    DELIVERY_FEE_PCT = 0.0003    # 0.03%
    FEE_CAP_PCT = 0.125          # 12.5% of option price

    # Spread parameters
    SPREAD_BASE = 0.01           # 1% ATM
    SPREAD_SLOPE = 0.04          # scales with moneyness distance
    SPREAD_CAP = 0.05            # 5% max spread
    SPREAD_LOWLIQ_MULT = 1.5     # multiplier when IV > 100%

    # Slippage parameters
    SLIPPAGE_BASE = 0.001        # 0.1% of notional
    SLIPPAGE_SCALE = 0.0005      # +0.05% per $1,000 above $5,000
    SLIPPAGE_THRESHOLD = 5000.0  # $ threshold for size-scaling
    SLIPPAGE_CAP = 0.005         # 0.5% max

    def _exchange_fee(self, spot: float, option_price: float,
                      btc_size: float, is_taker: bool = True) -> float:
        """Deribit exchange fee per leg, respecting the 12.5% cap."""
        fee_rate = self.TAKER_FEE_PCT if is_taker else self.MAKER_FEE_PCT
        raw_fee = fee_rate * spot * btc_size
        # Fee cap: 12.5% of option price (per BTC) * size
        cap = self.FEE_CAP_PCT * max(option_price, 1e-8) * btc_size
        return min(raw_fee, cap)

    def _spread_cost(self, spot: float, strike: float,
                     option_price: float, iv: float,
                     btc_size: float) -> float:
        """Half the bid-ask spread (cost to cross)."""
        moneyness = strike / spot if spot > 0 else 1.0
        spread_pct = self.SPREAD_BASE + self.SPREAD_SLOPE * abs(moneyness - 1.0)
        spread_pct = min(spread_pct, self.SPREAD_CAP)
        if iv > 1.0:  # vol > 100%
            spread_pct *= self.SPREAD_LOWLIQ_MULT
        # Half-spread cost (we pay half on entry, half on exit)
        return 0.5 * spread_pct * max(option_price, 1e-8) * btc_size

    def _slippage(self, notional: float) -> float:
        """Market-impact slippage."""
        slip_pct = self.SLIPPAGE_BASE
        if notional > self.SLIPPAGE_THRESHOLD:
            excess = notional - self.SLIPPAGE_THRESHOLD
            slip_pct += self.SLIPPAGE_SCALE * (excess / 1000.0)
        slip_pct = min(slip_pct, self.SLIPPAGE_CAP)
        return slip_pct * notional

    def calculate_entry_cost(self, spot: float, strike: float,
                             option_price: float, btc_size: float,
                             iv: float = 0.60,
                             is_taker: bool = True) -> float:
        """Total friction cost (USD) for opening one leg.

        Returns a positive number representing the drag on P&L.
        """
        notional = spot * btc_size
        fee = self._exchange_fee(spot, option_price, btc_size, is_taker)
        spread = self._spread_cost(spot, strike, option_price, iv, btc_size)
        slip = self._slippage(notional)
        return fee + spread + slip

    def calculate_exit_cost(self, spot: float, strike: float,
                            option_price: float, btc_size: float,
                            iv: float = 0.60,
                            is_taker: bool = True) -> float:
        """Total friction cost (USD) for closing one leg."""
        notional = spot * btc_size
        fee = self._exchange_fee(spot, option_price, btc_size, is_taker)
        spread = self._spread_cost(spot, strike, option_price, iv, btc_size)
        slip = self._slippage(notional)
        return fee + spread + slip

    def calculate_margin_requirement(self, spot: float, strike: float,
                                     option_type: str, direction: str,
                                     option_price: float,
                                     btc_size: float) -> float:
        """Deribit-style margin requirement (USD).

        Sold (short) options:
          max(0.15 * spot * btc_size,
              option_price * btc_size + 0.1 * spot * btc_size)
        Bought (long) options:
          Just the premium paid.
        """
        if direction == "short":
            margin_a = 0.15 * spot * btc_size
            margin_b = option_price * btc_size + 0.10 * spot * btc_size
            return max(margin_a, margin_b)
        else:  # long
            return option_price * btc_size

    def total_trade_friction(self, spot: float, legs: list,
                             iv: float = 0.60,
                             is_taker: bool = True) -> dict:
        """Compute aggregate friction for a multi-leg trade at entry.

        Returns dict with fees, spread, slippage, total, and margin.
        """
        total_fees = 0.0
        total_spread = 0.0
        total_slippage = 0.0
        total_margin = 0.0

        for leg in legs:
            strike = spot * leg.strike_pct
            btc_size = leg.size_btc
            opt_price = leg.entry_premium_usd

            notional = spot * btc_size
            fee = self._exchange_fee(spot, opt_price, btc_size, is_taker)
            spread = self._spread_cost(spot, strike, opt_price, iv, btc_size)
            slip = self._slippage(notional)
            margin = self.calculate_margin_requirement(
                spot, strike, leg.option_type, leg.direction,
                opt_price, btc_size
            )

            total_fees += fee
            total_spread += spread
            total_slippage += slip
            total_margin += margin

        return {
            "fees": total_fees,
            "spread": total_spread,
            "slippage": total_slippage,
            "total": total_fees + total_spread + total_slippage,
            "margin": total_margin,
        }

    def total_exit_friction(self, spot: float, legs: list,
                            exit_prices: list,
                            iv: float = 0.60,
                            is_taker: bool = True) -> dict:
        """Compute aggregate friction for closing a multi-leg trade.

        exit_prices: list of current option prices (same order as legs).
        """
        total_fees = 0.0
        total_spread = 0.0
        total_slippage = 0.0

        for leg, exit_px in zip(legs, exit_prices):
            strike = spot * leg.strike_pct  # approximate
            btc_size = leg.size_btc
            notional = spot * btc_size

            fee = self._exchange_fee(spot, exit_px, btc_size, is_taker)
            spread = self._spread_cost(spot, strike, exit_px, iv, btc_size)
            slip = self._slippage(notional)

            total_fees += fee
            total_spread += spread
            total_slippage += slip

        return {
            "fees": total_fees,
            "spread": total_spread,
            "slippage": total_slippage,
            "total": total_fees + total_spread + total_slippage,
        }






# =============================================================================
# 10.6 WOLVERINE RISK MANAGER - INSTITUTIONAL RISK CONTROLS
# =============================================================================

class WolverineRiskManager:
    """Institutional risk management layer for the options portfolio.

    Inspired by Wolverine Trading's approach to systematic risk controls.
    Implements pre-trade checks, circuit breakers, regime-based sizing,
    and correlation-aware position limits.

    All pre-trade methods return:
        (approved: bool, adjusted_size_multiplier: float, reason: str)
    """

    def __init__(self, initial_equity: float = 100.0):
        self.initial_equity = initial_equity
        self.current_equity = initial_equity
        self.peak_equity = initial_equity
        self.daily_pnl = 0.0
        self._last_date = None

        # --- Limits ---
        self.daily_loss_limit_pct = 0.05    # 5% of equity per day
        self.per_trade_max_loss_pct = 0.05  # 5% of equity per trade
        self.max_short_vol_exposure_pct = 0.06  # 6% combined short-vol cap

        # --- Open position tracking ---
        self.open_positions = []  # list of dicts

        # --- Circuit breaker state ---
        self.trading_halted = False
        self.long_only_mode = False
        self.size_halved = False

        # --- Stats ---
        self.rejection_log = []  # list of (date, strategy, reason)

    # -----------------------------------------------------------------
    # 1. update_equity
    # -----------------------------------------------------------------
    def update_equity(self, new_equity: float, date: str) -> float:
        """Update equity tracking and evaluate circuit breakers.

        Args:
            new_equity: Current portfolio equity.
            date: Current date string (YYYY-MM-DD).

        Returns:
            Current drawdown as a fraction (0.0 - 1.0).
        """
        self.current_equity = new_equity
        self.peak_equity = max(self.peak_equity, new_equity)

        # Reset daily P&L on new day
        if date != self._last_date:
            self.daily_pnl = 0.0
            self._last_date = date

        # Drawdown calculation
        drawdown_pct = (
            (self.peak_equity - self.current_equity) / self.peak_equity
            if self.peak_equity > 0 else 0.0
        )

        # --- Circuit breakers (escalating) ---
        # These are sticky: once tripped they stay until equity recovers
        if drawdown_pct > 0.30:
            self.trading_halted = True
            self.long_only_mode = True
            self.size_halved = True
        elif drawdown_pct > 0.25:
            self.trading_halted = False  # can trade again, but restricted
            self.long_only_mode = True
            self.size_halved = True
        elif drawdown_pct > 0.15:
            self.trading_halted = False
            self.long_only_mode = False
            self.size_halved = True
        else:
            # Recovery: all clear
            self.trading_halted = False
            self.long_only_mode = False
            self.size_halved = False

        return drawdown_pct

    # -----------------------------------------------------------------
    # 2. pre_trade_check
    # -----------------------------------------------------------------
    def pre_trade_check(
        self,
        signal,
        current_equity: float,
        current_regime: str,
    ) -> tuple:
        """Evaluate whether a trade should be taken.

        Args:
            signal: The TradeSignal from a strategy.
            current_equity: Current portfolio equity.
            current_regime: Current vol regime string.

        Returns:
            (approved, size_multiplier, reason)
        """
        size_mult = 1.0
        reasons = []

        # --- Hard rejects ---
        if self.trading_halted:
            return (False, 0.0, "HALTED: drawdown > 30%")

        # Determine if this trade has short legs (net short vol)
        has_short_legs = any(
            leg.direction == "short" for leg in signal.legs
        )
        is_net_short_vol = self._is_short_vol_trade(signal)

        if self.long_only_mode and has_short_legs:
            return (False, 0.0, "LONG-ONLY MODE: drawdown > 25%")

        # --- Daily loss limit ---
        if self.daily_pnl < -(current_equity * self.daily_loss_limit_pct):
            return (False, 0.0, "DAILY LOSS LIMIT: exceeded 5% daily loss")

        # --- Per-trade max loss check ---
        # NOTE: signal.max_loss_usd is PER BTC, not total trade risk.
        # Actual trade risk = max_loss_usd * position_size, which is handled
        # by PortfolioAllocator.MAX_EQUITY_AT_RISK after position sizing.
        # The risk manager focuses on circuit breakers, daily limits, regime
        # scaling, and short-vol concentration instead.

        # --- Regime-based sizing ---
        strategy_letter = signal.strategy_name.split("_")[0] if "_" in signal.strategy_name else ""
        regime_upper = current_regime.upper() if current_regime else ""

        regime_mult = self._regime_size_multiplier(
            strategy_letter, regime_upper, signal.strategy_name
        )
        if regime_mult <= 0:
            return (
                False, 0.0,
                "REGIME FILTER: {} rejected in {}".format(strategy_letter, regime_upper)
            )
        size_mult *= regime_mult
        if regime_mult < 1.0:
            reasons.append("regime scaling {:.2f}x in {}".format(regime_mult, regime_upper))

        # --- Correlation / concentration check ---
        # NOTE: The actual USD trade risk is not known here (position sizing
        # happens after this check in PortfolioAllocator). We limit the NUMBER
        # of concurrent short-vol positions instead. The PortfolioAllocator's
        # MAX_EQUITY_AT_RISK constraint handles the actual USD exposure cap.
        if is_net_short_vol:
            n_short_vol = sum(
                1 for p in self.open_positions
                if p["direction"] == "short_vol"
            )
            max_concurrent_short_vol = 2  # max 2 short-vol positions at once
            if n_short_vol >= max_concurrent_short_vol:
                return (
                    False, 0.0,
                    "SHORT-VOL CAP: {} short-vol positions already open".format(n_short_vol)
                )

        # --- Drawdown size halving (circuit breaker) ---
        if self.size_halved:
            size_mult *= 0.5
            reasons.append("size halved (DD > 15%)")

        # Clamp
        size_mult = max(size_mult, 0.01)  # never go below 1%

        reason_str = "APPROVED: " + ("; ".join(reasons) if reasons else "full size")
        return (True, size_mult, reason_str)

    # -----------------------------------------------------------------
    # 3. register_position
    # -----------------------------------------------------------------
    def register_position(
        self,
        signal,
        size_multiplier: float,
        actual_trade_risk_usd: float = 0.0,
    ) -> None:
        """Register a newly opened position for tracking.

        Args:
            signal: The TradeSignal.
            size_multiplier: Risk-manager scaling factor applied.
            actual_trade_risk_usd: Actual USD risk = max_loss_usd * position_size_btc.
                Computed by PortfolioAllocator after position sizing.
        """
        direction = (
            "short_vol"
            if self._is_short_vol_trade(signal)
            else "long_vol"
        )

        # Simplified net delta: sum of leg deltas
        net_delta = 0.0
        for leg in signal.legs:
            d = 0.50  # Simplified ATM delta
            if leg.option_type == "put":
                d = -0.50
            if leg.direction == "short":
                d = -d
            net_delta += d * leg.size_btc

        self.open_positions.append({
            "strategy_name": signal.strategy_name,
            "direction": direction,
            "max_loss": actual_trade_risk_usd,
            "entry_date": signal.timestamp,
            "net_delta": net_delta,
            "size_multiplier": size_multiplier,
        })

    # -----------------------------------------------------------------
    # 4. close_position
    # -----------------------------------------------------------------
    def close_position(self, strategy_name: str, pnl: float) -> None:
        """Remove a closed position and update daily P&L."""
        self.daily_pnl += pnl

        # Remove first matching position
        for i, pos in enumerate(self.open_positions):
            if pos["strategy_name"] == strategy_name:
                self.open_positions.pop(i)
                break

    # -----------------------------------------------------------------
    # 5. get_portfolio_summary
    # -----------------------------------------------------------------
    def get_portfolio_summary(self) -> dict:
        """Return current portfolio risk state."""
        n_open = len(self.open_positions)
        net_delta = sum(p["net_delta"] for p in self.open_positions)
        total_risk = sum(p["max_loss"] for p in self.open_positions)
        short_vol_risk = sum(
            p["max_loss"] for p in self.open_positions
            if p["direction"] == "short_vol"
        )
        long_vol_risk = sum(
            p["max_loss"] for p in self.open_positions
            if p["direction"] == "long_vol"
        )

        dd = (
            (self.peak_equity - self.current_equity) / self.peak_equity
            if self.peak_equity > 0 else 0.0
        )

        return {
            "n_open_positions": n_open,
            "net_delta": net_delta,
            "net_direction": (
                "short_vol" if short_vol_risk > long_vol_risk
                else "long_vol" if long_vol_risk > 0 else "flat"
            ),
            "total_max_loss_at_risk": total_risk,
            "short_vol_exposure": short_vol_risk,
            "long_vol_exposure": long_vol_risk,
            "drawdown_pct": dd,
            "trading_halted": self.trading_halted,
            "long_only_mode": self.long_only_mode,
            "size_halved": self.size_halved,
            "daily_pnl": self.daily_pnl,
        }

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------
    def _is_short_vol_trade(self, signal) -> bool:
        """Determine if a trade is net short volatility."""
        name = signal.strategy_name
        # Strategy A iron condors are short vol
        if "IronCondor" in name or "VolSelling" in name:
            return True
        # Strategy C PreEvent (sell straddle) is short vol
        if "PreEvent" in name:
            return True
        # Strategy D ShortVol is short vol
        if "ShortVol" in name:
            return True
        # Everything else is long vol
        return False

    def _regime_size_multiplier(
        self,
        strategy_letter: str,
        regime: str,
        strategy_name: str,
    ) -> float:
        """Return regime-based size multiplier for each strategy.

        DESIGN PHILOSOPHY: Strategies already have their own regime filters.
        The risk manager trusts those filters and only provides additional
        protection via circuit breakers and position limits.  Regime scaling
        is kept minimal to avoid eroding edge through double-filtering.

        Returns 0.0 to reject, < 1.0 to scale down, 1.0 for full size.
        """
        # All strategies already have robust regime-based entry filters.
        # The risk manager relies on circuit breakers (DD-based halving,
        # long-only mode, trading halt) for tail risk protection rather
        # than applying another regime overlay that would just scale down
        # already-filtered signals and reduce returns without improving Sharpe.
        return 1.0

# =============================================================================
# 11. PORTFOLIO ALLOCATOR - UNIFIED MULTI-STRATEGY SIMULATOR
# =============================================================================

@dataclass
class ActivePosition:
    """Track an active position across the portfolio."""
    strategy_name: str
    entry_idx: int
    entry_spot: float
    entry_date: str
    entry_regime: str
    legs: List[OptionLeg]
    position_size: float
    original_size: float
    entry_premium_per_btc: float   # Net premium (credit for short, debit for long)
    expiry_days: int
    is_short: bool                 # True for credit strategies, False for debit
    signal: TradeSignal
    max_value_ratio: float = 1.0
    partial_taken: bool = False
    risk_at_entry: float = 0.0     # USD risk at entry


class PortfolioAllocator:
    """
    Unified Portfolio Allocator running all 4 strategies simultaneously.

    Risk Controls:
      - Max 15% of equity at risk across all positions
      - Max 3 concurrent positions
      - Drawdown > 10%: halve all position sizes
      - Drawdown > 20%: only long vol strategies allowed (B, C2, D2)
      - Drawdown > 30%: halt all trading

    Processes each day:
      1. Update equity and drawdown
      2. Reprice and manage active positions (check exits)
      3. Check new entry signals from all 4 strategies
      4. Apply portfolio-level risk limits
      5. Record daily equity
    """

    MAX_EQUITY_AT_RISK = 0.15     # 15% max portfolio risk
    MAX_POSITIONS = 3
    DD_HALF_SIZE = 0.10           # 10% drawdown -> halve sizes
    DD_LONG_ONLY = 0.20           # 20% drawdown -> long vol only
    DD_HALT = 0.30                # 30% drawdown -> halt all

    def __init__(self, df: pd.DataFrame, initial_equity: float = 100.0,
                 friction_model: "FrictionModel" = None,
                 use_friction: bool = True,
                 risk_manager: "WolverineRiskManager" = None,
                 use_risk_mgmt: bool = False):
        self.df = df.copy()
        self.initial_equity = initial_equity
        self.friction_model = friction_model if friction_model is not None else FrictionModel()
        self.use_friction = use_friction
        self.risk_manager = risk_manager if risk_manager is not None else WolverineRiskManager(initial_equity)
        self.use_risk_mgmt = use_risk_mgmt
        self._precompute_features()

    def _precompute_features(self):
        """Pre-compute derived features needed by strategies."""
        # VoV 5-day average for Strategy A
        self.df["vov_30d_5d_avg"] = self.df["vov_30d"].rolling(5).mean()

        # Rolling percentile for rv_cc_30d (expanding window, min 90 days)
        # Use .rank(pct=True) on expanding window to avoid index mismatch
        rv = self.df["rv_cc_30d"]
        pctl = rv.expanding(min_periods=90).rank(pct=True) * 100
        self.df["rv_cc_30d_percentile"] = pctl

        # Previous day rv_cc_30d for decline detection
        self.df["rv_cc_30d_prev"] = self.df["rv_cc_30d"].shift(1)

    def _is_long_vol_strategy(self, strategy_name: str) -> bool:
        """Check if a strategy is long vol (allowed during high drawdown)."""
        long_vol_names = [
            "B_Momentum_LongOTM",
            "C_EventVol_PostEvent",
            "D_MeanReversion_LongVol",
        ]
        return any(lv in strategy_name for lv in long_vol_names)

    def run(self) -> Dict:
        """Run the full portfolio simulation."""
        # Initialize strategies
        strat_a = StrategyA_VolSelling()
        strat_b = StrategyB_MomentumBreakout()
        strat_c = StrategyC_EventVol()
        strat_d = StrategyD_MeanReversion()

        equity = self.initial_equity
        peak_equity = equity
        all_results: List[TradeResult] = []
        active_positions: List[ActivePosition] = []
        daily_equity = []
        daily_dates = []

        # Friction tracking
        cumulative_fees = 0.0
        cumulative_spread = 0.0
        cumulative_slippage = 0.0
        trades_killed_by_margin = 0

        # Risk management tracking
        risk_rejections = {}  # {reason: count}
        trades_risk_scaled = 0
        trades_risk_rejected = 0

        n = len(self.df)
        for idx in range(n):
            row = self.df.iloc[idx]
            date_str = str(row["date"])[:10]
            spot = row["close"]
            iv = row.get("iv_synthetic_30d", 0.60)
            if pd.isna(iv) or iv <= 0:
                iv = 0.60

            # --- Update drawdown ---
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0

            # --- Risk manager equity update ---
            if self.use_risk_mgmt:
                self.risk_manager.update_equity(equity, date_str)

            # --- Manage active positions (reprice + check exits) ---
            closed_indices = []
            for pos_idx, pos in enumerate(active_positions):
                days_held = idx - pos.entry_idx
                dte = pos.expiry_days - days_held

                # Reprice
                pnl_per_btc = 0.0
                if pos.is_short:
                    # Short straddle / iron condor
                    if "IronCondor" in pos.strategy_name:
                        pnl_per_btc = strat_a.reprice_position(
                            spot, iv, dte, pos.legs, pos.entry_spot
                        )
                    else:
                        # Short straddle (C1, D1)
                        T = max(dte, 0) / 365.0
                        for leg in pos.legs:
                            K = pos.entry_spot * leg.strike_pct
                            cp = bs_price(spot, K, T, 0, iv, leg.option_type)
                            pnl_per_btc += leg.entry_premium_usd - cp
                else:
                    # Long positions
                    T = max(dte, 0) / 365.0
                    for leg in pos.legs:
                        K = pos.entry_spot * leg.strike_pct
                        cp = bs_price(spot, K, T, 0, iv, leg.option_type)
                        pnl_per_btc += cp - leg.entry_premium_usd

                total_pnl = pnl_per_btc * pos.position_size

                # Track value ratio for trailing stops
                if not pos.is_short and pos.entry_premium_per_btc > 0:
                    # For long positions: current value / entry premium
                    T_curr = max(dte, 0) / 365.0
                    current_value = 0.0
                    for leg in pos.legs:
                        K = pos.entry_spot * leg.strike_pct
                        current_value += bs_price(spot, K, T_curr, 0, iv, leg.option_type)
                    value_ratio = current_value / max(pos.entry_premium_per_btc, 1e-10)
                    pos.max_value_ratio = max(pos.max_value_ratio, value_ratio)
                else:
                    value_ratio = 1.0

                # --- Check exit conditions ---
                exit_reason = None

                if pos.is_short:
                    # Short position exits
                    entry_credit = pos.entry_premium_per_btc
                    # TP: P&L > TP% of credit
                    if pnl_per_btc >= entry_credit * pos.signal.take_profit_pct:
                        exit_reason = "take_profit"
                    # SL: loss exceeds SL% of credit
                    elif pos.signal.stop_loss_pct < 1.0:
                        # SL as fraction of credit lost
                        if pnl_per_btc <= -entry_credit * pos.signal.stop_loss_pct:
                            exit_reason = "stop_loss"
                    else:
                        # SL as multiple of credit lost
                        if pnl_per_btc <= -entry_credit * pos.signal.stop_loss_pct:
                            exit_reason = "stop_loss"

                    # Regime exit for Strategy A
                    if "IronCondor" in pos.strategy_name:
                        curr_regime = row.get("regime_threshold")
                        if curr_regime != "CRISIS" and not pd.isna(curr_regime):
                            exit_reason = "regime_exit"
                else:
                    # Long position exits
                    # TP: value_ratio >= 1 + TP_mult
                    if value_ratio >= 1 + pos.signal.take_profit_pct:
                        exit_reason = "take_profit"
                    # SL: value dropped below floor
                    elif value_ratio <= pos.signal.stop_loss_pct:
                        exit_reason = "stop_loss"
                    # Partial exit for Strategy B
                    if ("LongOTM" in pos.strategy_name
                            and not pos.partial_taken
                            and value_ratio >= 2.0):
                        # Take partial profit
                        close_frac = 0.5
                        close_size = pos.original_size * close_frac
                        partial_pnl_per_btc = pnl_per_btc
                        partial_pnl = partial_pnl_per_btc * close_size

                        # Friction on partial exit
                        if self.use_friction:
                            partial_exit_prices = []
                            T_pe = max(dte, 0) / 365.0
                            for leg in pos.legs:
                                K = pos.entry_spot * leg.strike_pct
                                # Scale to partial size
                                cp = bs_price(spot, K, T_pe, 0, iv, leg.option_type)
                                partial_exit_prices.append(cp)
                            # Create temporary legs with partial sizes
                            import copy
                            partial_legs = [copy.copy(l) for l in pos.legs]
                            for pl in partial_legs:
                                pl.size_btc = close_size
                            fri = self.friction_model.total_exit_friction(
                                spot, partial_legs, partial_exit_prices, iv=iv
                            )
                            partial_pnl -= fri["total"]
                            cumulative_fees += fri["fees"]
                            cumulative_spread += fri["spread"]
                            cumulative_slippage += fri["slippage"]

                        equity += partial_pnl
                        pos.position_size -= close_size
                        pos.partial_taken = True

                        all_results.append(TradeResult(
                            strategy_name=pos.strategy_name,
                            entry_date=pos.entry_date,
                            exit_date=date_str,
                            entry_price_btc=pos.entry_spot,
                            exit_price_btc=spot,
                            pnl_usd=partial_pnl,
                            pnl_pct=partial_pnl / max(equity - partial_pnl, 1e-6),
                            holding_days=days_held,
                            exit_reason="partial_exit_200pct",
                            regime_at_entry=pos.entry_regime,
                            equity_before=equity - partial_pnl,
                            equity_after=equity,
                            max_premium_value=pos.max_value_ratio,
                        ))

                    # Trailing stop for Strategy B
                    if ("LongOTM" in pos.strategy_name
                            and pos.max_value_ratio >= 2.5
                            and exit_reason is None):
                        peak_gain = pos.max_value_ratio - 1.0
                        curr_gain = value_ratio - 1.0
                        if curr_gain <= peak_gain * 0.5:
                            exit_reason = "trailing_stop"

                # Time stop / expiry
                if dte <= pos.signal.time_stop_days and exit_reason is None:
                    exit_reason = "time_stop"
                if dte <= 0 and exit_reason is None:
                    exit_reason = "expiry"

                # === Execute exit ===
                if exit_reason:
                    total_pnl = pnl_per_btc * pos.position_size

                    # --- Friction at exit ---
                    exit_friction_usd = 0.0
                    if self.use_friction:
                        # Compute current option prices for each leg
                        exit_prices = []
                        T_exit = max(dte, 0) / 365.0
                        for leg in pos.legs:
                            K = pos.entry_spot * leg.strike_pct
                            cp = bs_price(spot, K, T_exit, 0, iv, leg.option_type)
                            exit_prices.append(cp)
                        fri = self.friction_model.total_exit_friction(
                            spot, pos.legs, exit_prices, iv=iv, is_taker=True
                        )
                        exit_friction_usd = fri["total"]
                        cumulative_fees += fri["fees"]
                        cumulative_spread += fri["spread"]
                        cumulative_slippage += fri["slippage"]
                        total_pnl -= exit_friction_usd

                    equity += total_pnl
                    pnl_pct = total_pnl / max(equity - total_pnl, 1e-6)

                    all_results.append(TradeResult(
                        strategy_name=pos.strategy_name,
                        entry_date=pos.entry_date,
                        exit_date=date_str,
                        entry_price_btc=pos.entry_spot,
                        exit_price_btc=spot,
                        pnl_usd=total_pnl,
                        pnl_pct=pnl_pct,
                        holding_days=days_held,
                        exit_reason=exit_reason,
                        regime_at_entry=pos.entry_regime,
                        equity_before=equity - total_pnl,
                        equity_after=equity,
                        max_premium_value=pos.max_value_ratio if not pos.is_short else 0.0,
                    ))

                    # Notify risk manager of closed position
                    if self.use_risk_mgmt:
                        self.risk_manager.close_position(pos.strategy_name, total_pnl)

                    # Update strategy kill switches
                    base_name = pos.strategy_name.split("_")[0] + "_" + pos.strategy_name.split("_")[1]
                    if total_pnl < 0:
                        if "VolSelling" in pos.strategy_name:
                            strat_a.record_loss(); strat_a.check_kill_switch(idx)
                        elif "Momentum" in pos.strategy_name:
                            strat_b.record_loss()
                        elif "EventVol" in pos.strategy_name:
                            strat_c.record_loss(); strat_c.check_kill_switch(idx)
                        elif "MeanReversion" in pos.strategy_name:
                            strat_d.record_loss(); strat_d.check_kill_switch(idx)
                    else:
                        if "VolSelling" in pos.strategy_name:
                            strat_a.record_win()
                        elif "Momentum" in pos.strategy_name:
                            strat_b.record_win()
                        elif "EventVol" in pos.strategy_name:
                            strat_c.record_win()
                        elif "MeanReversion" in pos.strategy_name:
                            strat_d.record_win()

                    closed_indices.append(pos_idx)

            # Remove closed positions (reverse order to preserve indices)
            for ci in sorted(closed_indices, reverse=True):
                active_positions.pop(ci)

            # --- Check new entry signals (skip if drawdown > 30%) ---
            if drawdown < self.DD_HALT and len(active_positions) < self.MAX_POSITIONS:
                # Calculate current risk
                current_risk = sum(p.risk_at_entry for p in active_positions)
                risk_budget_remaining = self.MAX_EQUITY_AT_RISK * equity - current_risk

                if risk_budget_remaining > 0:
                    # Determine active strategy names to avoid duplicate entries
                    active_strat_names = set()
                    for p in active_positions:
                        # Extract base strategy letter (A, B, C, D)
                        parts = p.strategy_name.split("_")
                        active_strat_names.add(parts[0] if parts else p.strategy_name)

                    # Collect signals from all strategies
                    candidates = []

                    # Strategy A
                    if "A" not in active_strat_names:
                        sig_a = strat_a.check_entry(row, idx, has_active_position=False)
                        if sig_a:
                            candidates.append((sig_a, strat_a, True))  # (signal, strategy, is_short)

                    # Strategy B
                    if "B" not in active_strat_names:
                        sig_b = strat_b.check_entry(row, idx, self.df, has_active_position=False)
                        if sig_b:
                            candidates.append((sig_b, strat_b, False))

                    # Strategy C
                    if "C" not in active_strat_names:
                        sig_c = strat_c.check_entry(row, idx, has_active_position=False)
                        if sig_c:
                            is_short_c = "PreEvent" in sig_c.strategy_name
                            candidates.append((sig_c, strat_c, is_short_c))

                    # Strategy D
                    if "D" not in active_strat_names:
                        sig_d = strat_d.check_entry(row, idx, has_active_position=False)
                        if sig_d:
                            is_short_d = "ShortVol" in sig_d.strategy_name
                            candidates.append((sig_d, strat_d, is_short_d))

                    # Apply drawdown filters
                    if drawdown >= self.DD_LONG_ONLY:
                        # Only allow long vol strategies
                        candidates = [(s, st, sh) for s, st, sh in candidates
                                      if self._is_long_vol_strategy(s.strategy_name)]

                    # Sort by confidence (highest first)
                    candidates.sort(key=lambda x: x[0].confidence, reverse=True)

                    # Execute up to remaining slots
                    for signal, strategy, is_short in candidates:
                        if len(active_positions) >= self.MAX_POSITIONS:
                            break

                        # --- Risk Manager Pre-Trade Check ---
                        if self.use_risk_mgmt:
                            current_regime_str = str(row.get("regime_threshold", ""))
                            approved, risk_size_mult, risk_reason = (
                                self.risk_manager.pre_trade_check(
                                    signal, equity, current_regime_str
                                )
                            )
                            if not approved:
                                risk_rejections[risk_reason] = (
                                    risk_rejections.get(risk_reason, 0) + 1
                                )
                                trades_risk_rejected += 1
                                self.risk_manager.rejection_log.append(
                                    (date_str, signal.strategy_name, risk_reason)
                                )
                                continue
                        else:
                            risk_size_mult = 1.0

                        # Size the trade
                        raw_size = strategy.size_trade(signal, equity, spot)
                        if raw_size <= 0:
                            continue

                        # Apply risk manager size adjustment
                        if self.use_risk_mgmt and risk_size_mult < 1.0:
                            raw_size *= risk_size_mult
                            trades_risk_scaled += 1

                        # Apply drawdown size reduction
                        if drawdown >= self.DD_HALF_SIZE:
                            raw_size *= 0.5

                        # Compute risk for this trade
                        trade_risk = signal.max_loss_usd * raw_size
                        if current_risk + trade_risk > self.MAX_EQUITY_AT_RISK * equity:
                            # Scale down to fit
                            max_trade_risk = self.MAX_EQUITY_AT_RISK * equity - current_risk
                            if max_trade_risk <= 0:
                                break
                            raw_size = max_trade_risk / signal.max_loss_usd

                        trade_risk = signal.max_loss_usd * raw_size

                        # Set leg sizes
                        for leg in signal.legs:
                            leg.size_btc = raw_size

                        # Calculate entry premium per BTC
                        if is_short:
                            entry_prem = sum(l.entry_premium_usd for l in signal.legs
                                             if l.direction == "short") -                                          sum(l.entry_premium_usd for l in signal.legs
                                             if l.direction == "long")
                        else:
                            entry_prem = sum(l.entry_premium_usd for l in signal.legs)

                        # --- Friction at entry ---
                        entry_friction_usd = 0.0
                        if self.use_friction:
                            fri = self.friction_model.total_trade_friction(
                                spot, signal.legs, iv=iv, is_taker=True
                            )
                            entry_friction_usd = fri["total"]
                            # Margin check: ensure margin fits within risk budget
                            margin_req = fri["margin"]
                            if margin_req > equity * self.MAX_EQUITY_AT_RISK:
                                trades_killed_by_margin += 1
                                continue  # skip this trade
                            # Deduct entry friction from equity
                            equity -= entry_friction_usd
                            cumulative_fees += fri["fees"]
                            cumulative_spread += fri["spread"]
                            cumulative_slippage += fri["slippage"]

                        active_positions.append(ActivePosition(
                            strategy_name=signal.strategy_name,
                            entry_idx=idx,
                            entry_spot=spot,
                            entry_date=date_str,
                            entry_regime=str(row.get("regime_threshold", "")),
                            legs=signal.legs,
                            position_size=raw_size,
                            original_size=raw_size,
                            entry_premium_per_btc=entry_prem,
                            expiry_days=signal.legs[0].expiry_days,
                            is_short=is_short,
                            signal=signal,
                            risk_at_entry=trade_risk,
                        ))

                        # Register with risk manager
                        if self.use_risk_mgmt:
                            self.risk_manager.register_position(
                                signal, risk_size_mult,
                                actual_trade_risk_usd=trade_risk,
                            )

                        current_risk += trade_risk

            # Record daily equity
            daily_equity.append(equity)
            daily_dates.append(date_str)

        # --- Close any remaining open positions ---
        for pos in active_positions:
            row = self.df.iloc[-1]
            spot = row["close"]
            days_held = n - 1 - pos.entry_idx
            dte = max(pos.expiry_days - days_held, 0)
            T = dte / 365.0
            iv_eod = row.get("iv_synthetic_30d", 0.60)
            if pd.isna(iv_eod) or iv_eod <= 0:
                iv_eod = 0.60

            pnl_per_btc = 0.0
            for leg in pos.legs:
                K = pos.entry_spot * leg.strike_pct
                cp = bs_price(spot, K, T, 0, iv_eod, leg.option_type)
                if leg.direction == "short":
                    pnl_per_btc += leg.entry_premium_usd - cp
                else:
                    pnl_per_btc += cp - leg.entry_premium_usd

            total_pnl = pnl_per_btc * pos.position_size

            # Friction on end-of-data close
            if self.use_friction:
                eod_exit_prices = []
                for leg in pos.legs:
                    K = pos.entry_spot * leg.strike_pct
                    cp = bs_price(spot, K, T, 0, iv_eod, leg.option_type)
                    eod_exit_prices.append(cp)
                fri = self.friction_model.total_exit_friction(
                    spot, pos.legs, eod_exit_prices, iv=iv_eod
                )
                total_pnl -= fri["total"]
                cumulative_fees += fri["fees"]
                cumulative_spread += fri["spread"]
                cumulative_slippage += fri["slippage"]

            equity += total_pnl

            all_results.append(TradeResult(
                strategy_name=pos.strategy_name,
                entry_date=pos.entry_date,
                exit_date=str(row["date"])[:10],
                entry_price_btc=pos.entry_spot,
                exit_price_btc=spot,
                pnl_usd=total_pnl,
                pnl_pct=total_pnl / max(equity - total_pnl, 1e-6),
                holding_days=days_held,
                exit_reason="end_of_data",
                regime_at_entry=pos.entry_regime,
                equity_before=equity - total_pnl,
                equity_after=equity,
            ))

        # Update the last daily equity entry to include end-of-data closes
        if daily_equity:
            daily_equity[-1] = equity

        # --- Build results ---
        equity_df = pd.DataFrame({
            "date": daily_dates,
            "equity": daily_equity,
        })
        equity_df["peak_equity"] = equity_df["equity"].cummax()
        equity_df["drawdown_pct"] = (
            (equity_df["peak_equity"] - equity_df["equity"]) / equity_df["peak_equity"] * 100
        )

        # Per-strategy breakdown
        strategy_groups = {}
        for r in all_results:
            # Group by base strategy name (A, B, C, D)
            base = r.strategy_name.split("_")[0]
            if base not in strategy_groups:
                strategy_groups[base] = []
            strategy_groups[base].append(r)

        return {
            "all_results": all_results,
            "strategy_groups": strategy_groups,
            "equity_df": equity_df,
            "final_equity": equity,
            "initial_equity": self.initial_equity,
            "friction_stats": {
                "total_fees": cumulative_fees,
                "total_spread": cumulative_spread,
                "total_slippage": cumulative_slippage,
                "total_friction": cumulative_fees + cumulative_spread + cumulative_slippage,
                "trades_killed_by_margin": trades_killed_by_margin,
                "use_friction": self.use_friction,
            },
            "risk_mgmt_stats": {
                "use_risk_mgmt": self.use_risk_mgmt,
                "trades_rejected": trades_risk_rejected,
                "trades_scaled": trades_risk_scaled,
                "rejection_breakdown": dict(risk_rejections),
                "portfolio_summary": (
                    self.risk_manager.get_portfolio_summary()
                    if self.use_risk_mgmt else {}
                ),
            },
        }


# =============================================================================
# 12. UPDATED MAIN - 3-WAY PORTFOLIO COMPARISON
# =============================================================================

def main():
    """Run 3-way backtest: frictionless, friction-only, friction + risk management."""
    import os
    from collections import Counter

    # Locate data file
    candidates = [
        "btc_options_system/btc_master_dataset.csv",
        os.path.join(os.getcwd(), "btc_options_system", "btc_master_dataset.csv"),
    ]
    try:
        candidates.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "btc_master_dataset.csv"
        ))
    except NameError:
        pass

    data_path = None
    for p in candidates:
        if os.path.exists(p):
            data_path = p
            break
    if data_path is None:
        raise FileNotFoundError(
            "btc_master_dataset.csv not found. Tried: {}".format(candidates)
        )

    print("Loading data from: {}".format(data_path))
    df = pd.read_csv(data_path)
    print("Dataset: {} rows x {} columns".format(len(df), len(df.columns)))
    print("Date range: {} to {}".format(df['date'].iloc[0], df['date'].iloc[-1]))
    print("Price range: ${:,.0f} to ${:,.0f}".format(df['close'].min(), df['close'].max()))

    initial_eq = 100.0

    # === Run 1: Frictionless (baseline) ===
    print("\n" + "=" * 70)
    print("  RUN 1: FRICTIONLESS BACKTEST (baseline)")
    print("=" * 70)
    alloc_clean = PortfolioAllocator(
        df, initial_equity=initial_eq,
        use_friction=False, use_risk_mgmt=False,
    )
    out_clean = alloc_clean.run()

    # === Run 2: Friction only ===
    print("\n" + "=" * 70)
    print("  RUN 2: FRICTION-ONLY BACKTEST")
    print("=" * 70)
    alloc_friction = PortfolioAllocator(
        df, initial_equity=initial_eq,
        use_friction=True, use_risk_mgmt=False,
    )
    out_friction = alloc_friction.run()

    # === Run 3: Friction + Risk Management ===
    print("\n" + "=" * 70)
    print("  RUN 3: FRICTION + WOLVERINE RISK MANAGEMENT")
    print("=" * 70)
    risk_mgr = WolverineRiskManager(initial_equity=initial_eq)
    alloc_risk = PortfolioAllocator(
        df, initial_equity=initial_eq,
        use_friction=True, use_risk_mgmt=True,
        risk_manager=risk_mgr,
    )
    out_risk = alloc_risk.run()

    # === Compute stats for all 3 runs ===
    def _compute_portfolio_stats(output, label):
        all_results = output["all_results"]
        equity_df = output["equity_df"]
        final_eq_val = output["final_equity"]
        n_days = len(equity_df)
        years = n_days / 365.25

        total_return = (final_eq_val - initial_eq) / initial_eq * 100
        cagr = ((final_eq_val / initial_eq) ** (1.0 / years) - 1) * 100 if years > 0 else 0

        eq_arr = np.array(equity_df["equity"])
        daily_rets = np.diff(eq_arr) / eq_arr[:-1]
        daily_rets = daily_rets[np.isfinite(daily_rets)]
        sharpe = (daily_rets.mean() / daily_rets.std() * np.sqrt(252)) if daily_rets.std() > 0 else 0

        max_dd = equity_df["drawdown_pct"].max()
        total_trades = len(all_results)
        wins = sum(1 for r in all_results if r.pnl_usd > 0)
        win_rate = wins / total_trades if total_trades > 0 else 0

        return {
            "label": label,
            "final_equity": final_eq_val,
            "total_return": total_return,
            "cagr": cagr,
            "sharpe": sharpe,
            "max_dd": max_dd,
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": win_rate,
            "years": years,
            "n_days": n_days,
        }

    stats_clean = _compute_portfolio_stats(out_clean, "Frictionless")
    stats_friction = _compute_portfolio_stats(out_friction, "Friction Only")
    stats_risk = _compute_portfolio_stats(out_risk, "Friction + Risk Mgmt")

    # === Per-strategy breakdown for risk-managed run ===
    strategy_names = {
        "A": "Strategy A: Iron Condor Vol Selling",
        "B": "Strategy B: Momentum Breakout (OTM)",
        "C": "Strategy C: Event Vol Exploitation",
        "D": "Strategy D: Mean Reversion Vol Fade",
    }

    print("\n" + "=" * 70)
    print("  FRICTION + RISK MGMT -- PER-STRATEGY BREAKDOWN")
    print("=" * 70)
    for key in ["A", "B", "C", "D"]:
        results = out_risk["strategy_groups"].get(key, [])
        stats = compute_stats(results, initial_eq)
        print_report(strategy_names.get(key, "Strategy {}".format(key)), stats, results)

    # === 3-Way Comparison Table ===
    print("\n" + "=" * 80)
    print("  3-WAY BACKTEST COMPARISON")
    print("=" * 80)
    print()

    sc, sf, sr = stats_clean, stats_friction, stats_risk

    header = "  {:<28s} {:>14s} {:>14s} {:>14s}".format(
        "Metric", "Frictionless", "Friction Only", "Fric+RiskMgmt"
    )
    print(header)
    print("  {} {} {} {}".format("-" * 28, "-" * 14, "-" * 14, "-" * 14))

    print("  {:<28s} {:>14s} {:>14s} {:>14s}".format(
        "Final Equity",
        "${:.2f}".format(sc["final_equity"]),
        "${:.2f}".format(sf["final_equity"]),
        "${:.2f}".format(sr["final_equity"]),
    ))
    print("  {:<28s} {:>+13.2f}% {:>+13.2f}% {:>+13.2f}%".format(
        "Total Return", sc["total_return"], sf["total_return"], sr["total_return"]
    ))
    print("  {:<28s} {:>13.2f}% {:>13.2f}% {:>13.2f}%".format(
        "CAGR", sc["cagr"], sf["cagr"], sr["cagr"]
    ))
    print("  {:<28s} {:>14.2f} {:>14.2f} {:>14.2f}".format(
        "Sharpe Ratio", sc["sharpe"], sf["sharpe"], sr["sharpe"]
    ))
    print("  {:<28s} {:>13.2f}% {:>13.2f}% {:>13.2f}%".format(
        "Max Drawdown", sc["max_dd"], sf["max_dd"], sr["max_dd"]
    ))
    print("  {:<28s} {:>14d} {:>14d} {:>14d}".format(
        "Total Trades", sc["total_trades"], sf["total_trades"], sr["total_trades"]
    ))
    print("  {:<28s} {:>13.1%} {:>13.1%} {:>13.1%}".format(
        "Win Rate", sc["win_rate"], sf["win_rate"], sr["win_rate"]
    ))

    # Risk mgmt specific rows
    risk_stats = out_risk["risk_mgmt_stats"]
    print("  {:<28s} {:>14s} {:>14s} {:>14d}".format(
        "Trades Rejected (Risk)", "-", "-", risk_stats["trades_rejected"]
    ))
    print("  {:<28s} {:>14s} {:>14s} {:>14d}".format(
        "Trades Scaled (Risk)", "-", "-", risk_stats["trades_scaled"]
    ))

    # === Friction Breakdown (risk-managed run) ===
    fri = out_risk["friction_stats"]
    print("\n  {:^60s}".format("--- FRICTION COST BREAKDOWN (Risk-Managed Run) ---"))
    print("  {:<30s} {:>15s}".format("Exchange Fees", "${:.6f}".format(fri["total_fees"])))
    print("  {:<30s} {:>15s}".format("Bid-Ask Spread", "${:.6f}".format(fri["total_spread"])))
    print("  {:<30s} {:>15s}".format("Slippage", "${:.6f}".format(fri["total_slippage"])))
    print("  {:<30s} {:>15s}".format("TOTAL FRICTION", "${:.6f}".format(fri["total_friction"])))
    print("  {:<30s} {:>15d}".format("Trades Killed by Margin", fri["trades_killed_by_margin"]))

    # === Risk Manager Rejection Breakdown ===
    print("\n  {:^60s}".format("--- RISK MANAGER REJECTION BREAKDOWN ---"))
    rejections = risk_stats.get("rejection_breakdown", {})
    if rejections:
        for reason, count in sorted(rejections.items(), key=lambda x: -x[1]):
            print("  {:<50s} {:>5d}".format(reason, count))
        print("  {}".format("-" * 55))
        print("  {:<50s} {:>5d}".format("TOTAL REJECTIONS", sum(rejections.values())))
    else:
        print("  No trades rejected by risk manager.")

    # === Portfolio Summary at End ===
    ps = risk_stats.get("portfolio_summary", {})
    if ps:
        print("\n  {:^60s}".format("--- FINAL PORTFOLIO RISK STATE ---"))
        print("  {:<30s} {:>15d}".format("Open Positions", ps.get("n_open_positions", 0)))
        print("  {:<30s} {:>15s}".format("Net Direction", ps.get("net_direction", "flat")))
        print("  {:<30s} {:>15s}".format("Total Risk at Close",
              "${:.6f}".format(ps.get("total_max_loss_at_risk", 0))))
        print("  {:<30s} {:>14.2f}%".format("Final Drawdown",
              ps.get("drawdown_pct", 0) * 100))
        print("  {:<30s} {:>15s}".format("Trading Halted",
              str(ps.get("trading_halted", False))))
        print("  {:<30s} {:>15s}".format("Long-Only Mode",
              str(ps.get("long_only_mode", False))))
        print("  {:<30s} {:>15s}".format("Size Halved",
              str(ps.get("size_halved", False))))

    # === Improvement Summary ===
    print("\n  {:^60s}".format("--- RISK MANAGEMENT IMPACT ---"))
    # Compare friction-only vs friction+risk
    sharpe_delta = sr["sharpe"] - sf["sharpe"]
    dd_delta = sr["max_dd"] - sf["max_dd"]
    ret_delta = sr["total_return"] - sf["total_return"]
    print("  vs Friction-Only:")
    print("    Sharpe:     {:.2f} -> {:.2f}  ({:+.2f})".format(
        sf["sharpe"], sr["sharpe"], sharpe_delta))
    print("    Max DD:     {:.2f}% -> {:.2f}%  ({:+.2f}%)".format(
        sf["max_dd"], sr["max_dd"], dd_delta))
    print("    Return:     {:+.2f}% -> {:+.2f}%  ({:+.2f}%)".format(
        sf["total_return"], sr["total_return"], ret_delta))
    if sharpe_delta > 0:
        print("  >> Risk management IMPROVED risk-adjusted returns (higher Sharpe)")
    if dd_delta < 0:
        print("  >> Risk management REDUCED max drawdown")

    # === Save equity curves ===
    base_dir = os.path.dirname(data_path)

    clean_csv = os.path.join(base_dir, "btc_portfolio_backtest.csv")
    out_clean["equity_df"].to_csv(clean_csv, index=False)
    print("\n  Frictionless equity curve saved to: {}".format(clean_csv))

    friction_csv = os.path.join(base_dir, "btc_portfolio_backtest_realistic.csv")
    out_friction["equity_df"].to_csv(friction_csv, index=False)
    print("  Friction equity curve saved to: {}".format(friction_csv))

    risk_csv = os.path.join(base_dir, "btc_portfolio_backtest_riskmanaged.csv")
    out_risk["equity_df"].to_csv(risk_csv, index=False)
    print("  Risk-managed equity curve saved to: {}".format(risk_csv))

    return {
        "frictionless": out_clean,
        "friction_only": out_friction,
        "risk_managed": out_risk,
        "stats_clean": stats_clean,
        "stats_friction": stats_friction,
        "stats_risk": stats_risk,
    }


if __name__ == "__main__":
    output = main()
