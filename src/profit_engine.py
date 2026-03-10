"""BTC Options Adaptive Profit-Taking Engine
==============================================================================
Evaluates every open position each cycle and computes optimal exit decisions
using 7 independent risk/reward metrics. Designed for 3-minute scalping cycles
but works on any timeframe.

Metrics:
  1. Kelly-optimal exit fraction
  2. Theta burn rate
  3. MFE trailing stop
  4. Sortino-based exit
  5. CVaR risk gate
  6. Time-based decay curve
  7. Correlation hedge check

Interface contract (used by monitor.py v4.0):
  engine = ProfitTakingEngine(execution_engine=exec, trade_history=[...])
  evals  = engine.evaluate_all(open_positions, btc_price)  # list[ExitDecision]
  exits  = engine.execute_exits(action_evals)               # list[dict]

v1.0 - 2026-03-10
"""

import math
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Tuple


# ======================================================================
# Data classes
# ======================================================================

@dataclass
class ExitDecision:
    """Output of the profit-taking evaluation for a single position."""
    trade_id: str
    instrument: str
    strategy: str
    decision: str           # CLOSE_NOW | SCALE_OUT_50 | TIGHTEN_STOP | HOLD
    confidence: float       # 0.0 - 1.0
    primary_reason: str     # Which metric triggered
    scores: Dict[str, float] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PositionTracker:
    """Tracks running statistics for an open position across cycles."""
    trade_id: str
    entry_price_usd: float
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    pnl_history: List[float] = field(default_factory=list)
    last_price_usd: float = 0.0
    cycles_held: int = 0


# ======================================================================
# Configuration
# ======================================================================

DEFAULT_CONFIG = {
    # Metric 1: Kelly
    'kelly_fraction': 0.5,
    'kelly_min_trades': 5,
    'kelly_default_edge': 0.08,

    # Metric 2: Theta burn
    'theta_burn_threshold': 0.7,

    # Metric 3: MFE trailing stop
    'mfe_retrace_scalp': 0.25,
    'mfe_retrace_swing': 0.40,
    'mfe_min_excursion_usd': 5.0,

    # Metric 4: Sortino
    'sortino_exit_threshold': 0.5,
    'sortino_min_observations': 3,

    # Metric 5: CVaR
    'cvar_multiplier': 2.0,
    'cvar_confidence': 0.95,

    # Metric 6: Time-based decay
    'time_targets': {
        (0, 1): 0.15,
        (1, 7): 0.30,
        (7, 21): 0.50,
        (21, 999): 0.75,
    },

    # Metric 7: Correlation hedge check
    'hedge_divergence_threshold': 0.20,

    # Decision weights (sum to 1.0)
    'weights': {
        'kelly': 0.15,
        'theta_burn': 0.15,
        'mfe_trailing': 0.20,
        'sortino': 0.10,
        'cvar': 0.15,
        'time_decay': 0.15,
        'correlation': 0.10,
    },

    # Decision thresholds
    'close_now_threshold': 0.70,
    'scale_out_threshold': 0.50,
    'tighten_threshold': 0.35,
}


# ======================================================================
# Profit-Taking Engine
# ======================================================================

class ProfitTakingEngine:
    """Adaptive profit-taking engine for BTC options positions."""

    def __init__(self, execution_engine, trade_history: List[dict] = None,
                 config: dict = None):
        self.exec = execution_engine
        self.trade_history = trade_history or []
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.trackers: Dict[str, PositionTracker] = {}
        self._win_rate = 0.0
        self._avg_win = 0.0
        self._avg_loss = 0.0
        self._compute_kelly_stats()

    # ------------------------------------------------------------------
    # Kelly stats from history
    # ------------------------------------------------------------------

    def _compute_kelly_stats(self):
        completed = [t for t in self.trade_history
                     if t.get('status') == 'closed' and t.get('pnl_usd', 0) != 0]
        if len(completed) < self.config['kelly_min_trades']:
            self._win_rate = 0.55
            self._avg_win = 1.0
            self._avg_loss = 1.0
            return
        wins = [t['pnl_usd'] for t in completed if t['pnl_usd'] > 0]
        losses = [abs(t['pnl_usd']) for t in completed if t['pnl_usd'] < 0]
        self._win_rate = len(wins) / len(completed) if completed else 0.5
        self._avg_win = sum(wins) / len(wins) if wins else 1.0
        self._avg_loss = sum(losses) / len(losses) if losses else 1.0

    # ------------------------------------------------------------------
    # Position tracker management
    # ------------------------------------------------------------------

    def _get_tracker(self, trade) -> PositionTracker:
        tid = self._field(trade, 'trade_id', '')
        entry_usd = self._field(trade, 'entry_price_usd', 0)
        if tid not in self.trackers:
            self.trackers[tid] = PositionTracker(
                trade_id=tid, entry_price_usd=entry_usd)
        return self.trackers[tid]

    def _update_tracker(self, tracker: PositionTracker, current_price_usd: float):
        pnl = current_price_usd - tracker.entry_price_usd
        tracker.pnl_history.append(pnl)
        tracker.last_price_usd = current_price_usd
        tracker.cycles_held += 1
        if pnl > tracker.mfe_usd:
            tracker.mfe_usd = pnl
        if pnl < tracker.mae_usd:
            tracker.mae_usd = pnl

    # ------------------------------------------------------------------
    # Helper: extract fields from PaperTrade (dataclass or dict)
    # ------------------------------------------------------------------

    @staticmethod
    def _field(trade, name, default=None):
        if hasattr(trade, name):
            return getattr(trade, name)
        if isinstance(trade, dict):
            return trade.get(name, default)
        return default

    # ------------------------------------------------------------------
    # Metric 1: Kelly-Optimal Exit Fraction
    # ------------------------------------------------------------------

    def _score_kelly(self, trade, current_price_usd: float) -> Tuple[float, dict]:
        entry_usd = self._field(trade, 'entry_price_usd', 0)
        if entry_usd <= 0:
            return 0.0, {'reason': 'no_entry_price'}
        pnl_pct = (current_price_usd - entry_usd) / entry_usd
        if self._avg_win > 0:
            b = self._avg_win / self._avg_loss if self._avg_loss > 0 else 2.0
            kelly_f = (self._win_rate * b - (1 - self._win_rate)) / b
        else:
            kelly_f = self.config['kelly_default_edge']
        kelly_target = kelly_f * self.config['kelly_fraction']
        kelly_target = max(kelly_target, 0.03)
        if pnl_pct >= kelly_target:
            score = min(1.0, pnl_pct / kelly_target)
        elif pnl_pct > 0:
            score = 0.3 * (pnl_pct / kelly_target)
        else:
            score = 0.0
        return score, {
            'pnl_pct': round(pnl_pct, 4),
            'kelly_target': round(kelly_target, 4),
            'kelly_raw': round(kelly_f, 4),
            'win_rate': round(self._win_rate, 3),
        }

    # ------------------------------------------------------------------
    # Metric 2: Theta Burn Rate
    # ------------------------------------------------------------------

    def _score_theta_burn(self, trade, greeks: dict, current_price_usd: float) -> Tuple[float, dict]:
        theta = greeks.get('theta', 0)
        entry_usd = self._field(trade, 'entry_price_usd', 0)
        direction = self._field(trade, 'direction', 'buy')
        if entry_usd <= 0 or theta == 0:
            return 0.0, {'reason': 'insufficient_data'}
        is_long = direction == 'buy'
        theta_cost_per_day = abs(theta) if is_long else 0
        remaining_edge = max(current_price_usd - entry_usd, 0.001)
        if is_long and theta_cost_per_day > 0:
            burn_ratio = theta_cost_per_day / remaining_edge
            if burn_ratio >= self.config['theta_burn_threshold']:
                score = min(1.0, burn_ratio)
            else:
                score = 0.2 * (burn_ratio / self.config['theta_burn_threshold'])
        else:
            score = 0.0
        return score, {
            'theta': round(theta, 6),
            'theta_cost_per_day': round(theta_cost_per_day, 4),
            'remaining_edge': round(remaining_edge, 4),
            'is_long': is_long,
        }

    # ------------------------------------------------------------------
    # Metric 3: MFE Trailing Stop
    # ------------------------------------------------------------------

    def _score_mfe_trailing(self, trade, tracker: PositionTracker,
                            current_price_usd: float) -> Tuple[float, dict]:
        mfe = tracker.mfe_usd
        if mfe < self.config['mfe_min_excursion_usd']:
            return 0.0, {'reason': 'mfe_too_small', 'mfe': round(mfe, 2)}
        current_pnl = current_price_usd - tracker.entry_price_usd
        retrace = (mfe - current_pnl) / mfe if mfe > 0 else 0
        expiry_str = self._field(trade, 'expiry', '')
        dte = self._calc_dte(expiry_str)
        threshold = (self.config['mfe_retrace_scalp'] if dte <= 1
                     else self.config['mfe_retrace_swing'])
        if retrace >= threshold:
            score = min(1.0, retrace / threshold)
        elif retrace > threshold * 0.5:
            score = 0.3 * (retrace / threshold)
        else:
            score = 0.0
        return score, {
            'mfe_usd': round(mfe, 2),
            'current_pnl': round(current_pnl, 2),
            'retrace_pct': round(retrace, 4),
            'threshold': round(threshold, 4),
            'dte': round(dte, 2),
        }

    # ------------------------------------------------------------------
    # Metric 4: Sortino-Based Exit
    # ------------------------------------------------------------------

    def _score_sortino(self, tracker: PositionTracker) -> Tuple[float, dict]:
        pnl_hist = tracker.pnl_history
        if len(pnl_hist) < self.config['sortino_min_observations']:
            return 0.0, {'reason': 'insufficient_data', 'observations': len(pnl_hist)}
        returns = []
        for i in range(1, len(pnl_hist)):
            returns.append(pnl_hist[i] - pnl_hist[i - 1])
        if not returns:
            return 0.0, {'reason': 'no_returns'}
        avg_return = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return 0.0, {'sortino': 999, 'reason': 'no_downside'}
        downside_var = sum(r ** 2 for r in downside) / len(downside)
        downside_dev = math.sqrt(downside_var) if downside_var > 0 else 0.001
        sortino = avg_return / downside_dev
        threshold = self.config['sortino_exit_threshold']
        if sortino < threshold:
            score = min(1.0, (threshold - sortino) / threshold)
        else:
            score = 0.0
        return score, {
            'sortino': round(sortino, 3),
            'avg_return': round(avg_return, 4),
            'downside_dev': round(downside_dev, 4),
            'threshold': threshold,
        }

    # ------------------------------------------------------------------
    # Metric 5: CVaR Risk Gate
    # ------------------------------------------------------------------

    def _score_cvar(self, trade, tracker: PositionTracker) -> Tuple[float, dict]:
        pnl_hist = tracker.pnl_history
        if len(pnl_hist) < 3:
            return 0.0, {'reason': 'insufficient_data'}
        sorted_pnl = sorted(pnl_hist)
        cutoff_idx = max(1, int(len(sorted_pnl) * (1 - self.config['cvar_confidence'])))
        tail = sorted_pnl[:cutoff_idx]
        cvar = abs(sum(tail) / len(tail)) if tail else 0
        stop_loss = self._field(trade, 'stop_loss_price', 0)
        entry_usd = self._field(trade, 'entry_price_usd', 0)
        original_max_loss = abs(entry_usd - stop_loss) if stop_loss > 0 else entry_usd * 0.15
        original_max_loss = max(original_max_loss, 1.0)
        multiplier = self.config['cvar_multiplier']
        if cvar >= original_max_loss * multiplier:
            score = min(1.0, cvar / (original_max_loss * multiplier))
        elif cvar > original_max_loss:
            score = 0.3 * (cvar / (original_max_loss * multiplier))
        else:
            score = 0.0
        return score, {
            'cvar_95': round(cvar, 2),
            'original_max_loss': round(original_max_loss, 2),
            'ratio': round(cvar / original_max_loss, 3) if original_max_loss > 0 else 0,
        }

    # ------------------------------------------------------------------
    # Metric 6: Time-Based Decay Curve
    # ------------------------------------------------------------------

    def _score_time_decay(self, trade, current_price_usd: float) -> Tuple[float, dict]:
        entry_usd = self._field(trade, 'entry_price_usd', 0)
        if entry_usd <= 0:
            return 0.0, {'reason': 'no_entry_price'}
        expiry_str = self._field(trade, 'expiry', '')
        dte = self._calc_dte(expiry_str)
        pnl_pct = (current_price_usd - entry_usd) / entry_usd
        target = 0.50
        matched_bracket = None
        for (low, high), tgt in self.config['time_targets'].items():
            if low <= dte < high:
                target = tgt
                matched_bracket = (low, high)
                break
        if pnl_pct >= target:
            score = min(1.0, pnl_pct / target)
        elif pnl_pct > 0:
            score = 0.2 * (pnl_pct / target)
        else:
            score = 0.0
        return score, {
            'dte': round(dte, 2),
            'pnl_pct': round(pnl_pct, 4),
            'target_pct': round(target, 4),
            'bracket': str(matched_bracket),
        }

    # ------------------------------------------------------------------
    # Metric 7: Correlation Hedge Check
    # ------------------------------------------------------------------

    def _score_correlation(self, trade, greeks: dict,
                           btc_price: float, current_price_usd: float) -> Tuple[float, dict]:
        delta = greeks.get('delta', 0)
        entry_usd = self._field(trade, 'entry_price_usd', 0)
        if abs(delta) < 0.01 or entry_usd <= 0:
            return 0.0, {'reason': 'insufficient_delta'}
        option_move = current_price_usd - entry_usd
        implied_btc_move = option_move / delta if abs(delta) > 0.01 else 0
        gamma = greeks.get('gamma', 0)
        if abs(implied_btc_move) > 0 and abs(gamma) > 0:
            gamma_correction = 0.5 * gamma * implied_btc_move ** 2
            expected_option_move = delta * implied_btc_move + gamma_correction
        else:
            expected_option_move = delta * implied_btc_move
        if abs(expected_option_move) > 0:
            divergence = abs(option_move - expected_option_move) / abs(expected_option_move)
        else:
            divergence = 0
        threshold = self.config['hedge_divergence_threshold']
        if divergence >= threshold:
            score = min(1.0, divergence / threshold)
        else:
            score = 0.1 * (divergence / threshold)
        return score, {
            'delta': round(delta, 4),
            'gamma': round(gamma, 6),
            'option_move': round(option_move, 4),
            'expected_move': round(expected_option_move, 4),
            'divergence': round(divergence, 4),
        }

    # ------------------------------------------------------------------
    # Helper: DTE calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_dte(expiry_str: str) -> float:
        if not expiry_str:
            return 7.0
        try:
            now = datetime.now(timezone.utc)
            for fmt in ['%d%b%y', '%Y-%m-%d', '%d%b%Y']:
                try:
                    exp = datetime.strptime(expiry_str.upper(), fmt).replace(tzinfo=timezone.utc)
                    dte = (exp - now).total_seconds() / 86400
                    return max(dte, 0.001)
                except ValueError:
                    continue
            return 7.0
        except Exception:
            return 7.0

    # ------------------------------------------------------------------
    # Current price estimation
    # ------------------------------------------------------------------

    def _get_current_price(self, trade) -> float:
        instrument = self._field(trade, 'instrument', '')
        if not instrument:
            return self._field(trade, 'entry_price_usd', 0)
        try:
            ticker = self.exec._public_get('/public/ticker', {'instrument_name': instrument})
            if ticker and 'mark_price' in ticker:
                btc_idx = ticker.get('index_price', ticker.get('underlying_price', 80000))
                mark_btc = ticker['mark_price']
                return mark_btc * btc_idx
        except Exception:
            pass
        return self._field(trade, 'entry_price_usd', 0)

    # ==================================================================
    # MAIN EVALUATION
    # ==================================================================

    def evaluate_all(self, open_positions: list, btc_price: float) -> List[ExitDecision]:
        decisions = []
        for trade in open_positions:
            tid = self._field(trade, 'trade_id', '')
            instrument = self._field(trade, 'instrument', '')
            strategy = self._field(trade, 'strategy', '')
            current_price = self._get_current_price(trade)
            greeks = {}
            if instrument:
                try:
                    greeks = self.exec.get_position_greeks(instrument)
                except Exception:
                    greeks = {}
            tracker = self._get_tracker(trade)
            self._update_tracker(tracker, current_price)
            scores = {}
            details = {}
            s1, d1 = self._score_kelly(trade, current_price)
            scores['kelly'] = s1
            details['kelly'] = d1
            s2, d2 = self._score_theta_burn(trade, greeks, current_price)
            scores['theta_burn'] = s2
            details['theta_burn'] = d2
            s3, d3 = self._score_mfe_trailing(trade, tracker, current_price)
            scores['mfe_trailing'] = s3
            details['mfe_trailing'] = d3
            s4, d4 = self._score_sortino(tracker)
            scores['sortino'] = s4
            details['sortino'] = d4
            s5, d5 = self._score_cvar(trade, tracker)
            scores['cvar'] = s5
            details['cvar'] = d5
            s6, d6 = self._score_time_decay(trade, current_price)
            scores['time_decay'] = s6
            details['time_decay'] = d6
            s7, d7 = self._score_correlation(trade, greeks, btc_price, current_price)
            scores['correlation'] = s7
            details['correlation'] = d7
            weights = self.config['weights']
            composite = sum(
                scores.get(k, 0) * weights.get(k, 0)
                for k in weights
            )
            if composite >= self.config['close_now_threshold']:
                decision = 'CLOSE_NOW'
            elif composite >= self.config['scale_out_threshold']:
                decision = 'SCALE_OUT_50'
            elif composite >= self.config['tighten_threshold']:
                decision = 'TIGHTEN_STOP'
            else:
                decision = 'HOLD'
            primary = max(scores, key=lambda k: scores[k] * weights.get(k, 0))
            decisions.append(ExitDecision(
                trade_id=tid,
                instrument=instrument,
                strategy=strategy,
                decision=decision,
                confidence=round(composite, 4),
                primary_reason=primary,
                scores={k: round(v, 4) for k, v in scores.items()},
                details=details,
            ))
        return decisions

    # ==================================================================
    # EXIT EXECUTION
    # ==================================================================

    def execute_exits(self, action_evals: List[ExitDecision]) -> List[dict]:
        results = []
        for ev in action_evals:
            if ev.decision == 'HOLD':
                continue
            try:
                if ev.decision == 'CLOSE_NOW':
                    result = self.exec.close_position(
                        trade_id=ev.trade_id,
                        size_fraction=1.0,
                        reason=f'profit_engine:{ev.primary_reason}'
                    )
                elif ev.decision == 'SCALE_OUT_50':
                    result = self.exec.close_position(
                        trade_id=ev.trade_id,
                        size_fraction=0.5,
                        reason=f'profit_engine:scale_out:{ev.primary_reason}'
                    )
                elif ev.decision == 'TIGHTEN_STOP':
                    result = {
                        'action': 'tighten_stop',
                        'trade_id': ev.trade_id,
                        'instrument': ev.instrument,
                        'reason': ev.primary_reason,
                        'confidence': ev.confidence,
                    }
                else:
                    continue
                result_dict = {
                    'trade_id': ev.trade_id,
                    'instrument': ev.instrument,
                    'decision': ev.decision,
                    'reason': ev.primary_reason,
                    'confidence': ev.confidence,
                    'result': result,
                }
                results.append(result_dict)
                if ev.decision == 'CLOSE_NOW' and ev.trade_id in self.trackers:
                    del self.trackers[ev.trade_id]
                print(f'    {ev.decision} {ev.instrument} '
                      f'(reason: {ev.primary_reason}, conf: {ev.confidence:.2f})')
            except Exception as e:
                results.append({
                    'trade_id': ev.trade_id,
                    'decision': ev.decision,
                    'error': str(e),
                })
                print(f'    ERROR executing {ev.decision} for {ev.instrument}: {e}')
        return results

    # ==================================================================
    # DIAGNOSTICS
    # ==================================================================

    def get_diagnostic_summary(self) -> dict:
        return {
            'tracked_positions': len(self.trackers),
            'kelly_stats': {
                'win_rate': round(self._win_rate, 3),
                'avg_win': round(self._avg_win, 2),
                'avg_loss': round(self._avg_loss, 2),
                'trade_history_size': len(self.trade_history),
            },
            'trackers': {
                tid: {
                    'mfe': round(t.mfe_usd, 2),
                    'mae': round(t.mae_usd, 2),
                    'cycles': t.cycles_held,
                    'last_pnl': round(t.pnl_history[-1], 2) if t.pnl_history else 0,
                }
                for tid, t in self.trackers.items()
            },
        }
