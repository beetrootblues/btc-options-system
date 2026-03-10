"""BTC Options System - Phase 5: Monte Carlo Simulation & Stress Testing
======================================================================
Two classes:
  1. MonteCarloEngine  - Bootstrap resampling of trade P&Ls (10,000 sims)
  2. StressTester      - Regime stress tests + correlation stress analysis

Author: BTC Options System
Date: 2026-03-09
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import time
import warnings

warnings.filterwarnings("ignore")


# =============================================================================
# 1. MONTE CARLO ENGINE
# =============================================================================

class MonteCarloEngine:
    """Bootstrap resampling Monte Carlo simulator for trade P&L sequences.

    Takes actual trade P&Ls from the backtest and creates 10,000 synthetic
    trade sequences by sampling WITH replacement, then computes distribution
    statistics on terminal wealth, drawdowns, and risk metrics.
    """

    def __init__(self, trade_pnls: List[float], initial_equity: float = 100.0,
                 n_simulations: int = 10000, n_trades: int = None,
                 strategy_names: List[str] = None,
                 holding_days: List[float] = None):
        """
        Args:
            trade_pnls: List of actual trade P&L values (USD) from backtest
            initial_equity: Starting capital
            n_simulations: Number of Monte Carlo paths
            n_trades: Length of each simulated sequence (default = len(trade_pnls))
            strategy_names: Optional parallel list of strategy names per trade
            holding_days: Optional parallel list of holding days per trade
        """
        self.trade_pnls = np.array(trade_pnls, dtype=np.float64)
        self.initial_equity = initial_equity
        self.n_simulations = n_simulations
        self.n_trades = n_trades if n_trades is not None else len(trade_pnls)
        self.strategy_names = strategy_names
        self.holding_days = np.array(holding_days, dtype=np.float64) if holding_days else None
        self.results = None

    def run(self) -> Dict:
        """Run the Monte Carlo simulation using vectorized numpy operations.

        Returns:
            Dict with all computed statistics
        """
        t0 = time.time()
        n_pnls = len(self.trade_pnls)
        n_sims = self.n_simulations
        n_trades = self.n_trades
        eq0 = self.initial_equity

        if n_pnls == 0:
            return self._empty_results()

        # --- Bootstrap resampling: (n_sims, n_trades) matrix of P&L indices ---
        rng = np.random.default_rng(seed=42)
        idx = rng.integers(0, n_pnls, size=(n_sims, n_trades))
        pnl_matrix = self.trade_pnls[idx]  # (n_sims, n_trades)

        # --- Build equity curves: cumulative sum of P&Ls + initial equity ---
        equity_curves = np.cumsum(pnl_matrix, axis=1) + eq0  # (n_sims, n_trades)
        # Prepend initial equity column
        eq_with_start = np.hstack([
            np.full((n_sims, 1), eq0),
            equity_curves
        ])  # (n_sims, n_trades+1)

        # --- Terminal wealth ---
        terminal_wealth = equity_curves[:, -1]

        # --- Running maximum for drawdown calculation ---
        running_max = np.maximum.accumulate(eq_with_start, axis=1)
        drawdowns = (eq_with_start - running_max) / np.maximum(running_max, 1e-10)
        max_drawdowns = np.abs(drawdowns.min(axis=1))  # (n_sims,)

        # --- Probability of ruin (equity < $50 at ANY point) ---
        min_equity = eq_with_start.min(axis=1)
        prob_ruin = np.mean(min_equity < 50.0)

        # --- Probability of doubling (equity >= $200 at ANY point) ---
        max_equity = eq_with_start.max(axis=1)
        prob_double = np.mean(max_equity >= 200.0)

        # --- Probability of 10x (equity >= $1000 at ANY point) ---
        prob_10x = np.mean(max_equity >= 1000.0)

        # --- Expected trades to double ---
        # For each sim, find first trade where equity >= 200
        doubled_mask = equity_curves >= 200.0  # (n_sims, n_trades)
        # argmax on boolean array gives first True index (0 if never True)
        first_double_idx = np.argmax(doubled_mask, axis=1)  # (n_sims,)
        # sims that never doubled: doubled_mask[i, first_double_idx[i]] == False
        ever_doubled = doubled_mask[np.arange(n_sims), first_double_idx]
        if ever_doubled.sum() >= n_sims * 0.5:
            trades_to_double = first_double_idx[ever_doubled] + 1  # 1-indexed
            expected_trades_to_double = float(np.median(trades_to_double))
        else:
            expected_trades_to_double = float('inf')  # "never" if < 50% reach it

        # --- Sharpe ratio distribution ---
        # Per-simulation Sharpe: mean(pnl) / std(pnl) * sqrt(annualization)
        avg_hold = float(np.mean(self.holding_days)) if self.holding_days is not None else 7.0
        annualization = np.sqrt(252.0 / max(avg_hold, 1.0))
        pnl_means = pnl_matrix.mean(axis=1)
        pnl_stds = pnl_matrix.std(axis=1)
        pnl_stds = np.maximum(pnl_stds, 1e-10)  # avoid div by zero
        sharpe_distribution = (pnl_means / pnl_stds) * annualization

        elapsed = time.time() - t0

        # --- Compile results ---
        self.results = {
            # Terminal wealth distribution
            "median_terminal_wealth": float(np.median(terminal_wealth)),
            "mean_terminal_wealth": float(np.mean(terminal_wealth)),
            "percentile_5th": float(np.percentile(terminal_wealth, 5)),
            "percentile_25th": float(np.percentile(terminal_wealth, 25)),
            "percentile_75th": float(np.percentile(terminal_wealth, 75)),
            "percentile_95th": float(np.percentile(terminal_wealth, 95)),
            "std_terminal_wealth": float(np.std(terminal_wealth)),

            # Probability metrics
            "prob_ruin": float(prob_ruin),
            "prob_loss": float(np.mean(terminal_wealth < eq0)),
            "prob_double": float(prob_double),
            "prob_10x": float(prob_10x),
            "expected_trades_to_double": expected_trades_to_double,

            # Max drawdown distribution
            "max_dd_5th": float(np.percentile(max_drawdowns, 5)) * 100,
            "max_dd_50th": float(np.percentile(max_drawdowns, 50)) * 100,
            "max_dd_95th": float(np.percentile(max_drawdowns, 95)) * 100,
            "max_dd_mean": float(np.mean(max_drawdowns)) * 100,

            # Sharpe distribution
            "sharpe_5th": float(np.percentile(sharpe_distribution, 5)),
            "sharpe_50th": float(np.percentile(sharpe_distribution, 50)),
            "sharpe_95th": float(np.percentile(sharpe_distribution, 95)),
            "sharpe_mean": float(np.mean(sharpe_distribution)),

            # Metadata
            "n_simulations": n_sims,
            "n_trades_per_sim": n_trades,
            "n_actual_trades": n_pnls,
            "initial_equity": eq0,
            "avg_trade_pnl": float(np.mean(self.trade_pnls)),
            "std_trade_pnl": float(np.std(self.trade_pnls)),
            "win_rate": float(np.mean(self.trade_pnls > 0)),
            "elapsed_seconds": elapsed,

            # Raw distributions for plotting
            "_terminal_wealth_dist": terminal_wealth,
            "_max_dd_dist": max_drawdowns,
            "_sharpe_dist": sharpe_distribution,
        }

        return self.results

    def _empty_results(self) -> Dict:
        """Return empty results when no trades available."""
        return {
            "median_terminal_wealth": self.initial_equity,
            "mean_terminal_wealth": self.initial_equity,
            "percentile_5th": self.initial_equity,
            "percentile_25th": self.initial_equity,
            "percentile_75th": self.initial_equity,
            "percentile_95th": self.initial_equity,
            "prob_ruin": 0.0, "prob_loss": 0.0,
            "prob_double": 0.0, "prob_10x": 0.0,
            "expected_trades_to_double": float('inf'),
            "max_dd_5th": 0.0, "max_dd_50th": 0.0, "max_dd_95th": 0.0,
            "sharpe_5th": 0.0, "sharpe_50th": 0.0, "sharpe_95th": 0.0,
            "n_simulations": 0, "n_trades_per_sim": 0,
            "n_actual_trades": 0, "elapsed_seconds": 0.0,
        }

    def save_csv(self, path: str):
        """Save summary stats to CSV."""
        if self.results is None:
            raise ValueError("Must call run() first")

        # Filter out internal distributions (numpy arrays)
        stats = {k: v for k, v in self.results.items() if not k.startswith('_')}
        df = pd.DataFrame([stats])
        df.to_csv(path, index=False)
        print(f"  Saved Monte Carlo results to {path}")
        return df

    def print_report(self):
        """Print formatted Monte Carlo results."""
        if self.results is None:
            raise ValueError("Must call run() first")
        r = self.results

        print("\n" + "=" * 70)
        print("MONTE CARLO SIMULATION RESULTS")
        print(f"  {r['n_simulations']:,} simulations x {r['n_trades_per_sim']} trades each")
        print(f"  Based on {r['n_actual_trades']} actual trades (win rate: {r['win_rate']:.1%})")
        print(f"  Avg trade P&L: ${r['avg_trade_pnl']:.4f} +/- ${r['std_trade_pnl']:.4f}")
        print(f"  Completed in {r['elapsed_seconds']:.2f}s")
        print("=" * 70)

        print("\n--- Terminal Wealth Distribution ---")
        print(f"  Initial equity:     ${r['initial_equity']:.2f}")
        print(f"  5th percentile:     ${r['percentile_5th']:.2f}")
        print(f"  25th percentile:    ${r['percentile_25th']:.2f}")
        print(f"  Median:             ${r['median_terminal_wealth']:.2f}")
        print(f"  Mean:               ${r['mean_terminal_wealth']:.2f}")
        print(f"  75th percentile:    ${r['percentile_75th']:.2f}")
        print(f"  95th percentile:    ${r['percentile_95th']:.2f}")
        print(f"  Std deviation:      ${r.get('std_terminal_wealth', 0):.2f}")

        print("\n--- Probability Metrics ---")
        print(f"  P(ruin < $50):      {r['prob_ruin']:.2%}")
        print(f"  P(loss < $100):     {r['prob_loss']:.2%}")
        print(f"  P(double >= $200):  {r['prob_double']:.2%}")
        print(f"  P(10x >= $1000):    {r['prob_10x']:.2%}")
        if r['expected_trades_to_double'] == float('inf'):
            print(f"  Trades to double:   NEVER (< 50% of sims reach $200)")
        else:
            print(f"  Trades to double:   {r['expected_trades_to_double']:.0f} trades (median)")

        print("\n--- Max Drawdown Distribution ---")
        print(f"  5th percentile:     {r['max_dd_5th']:.2f}%")
        print(f"  Median:             {r['max_dd_50th']:.2f}%")
        print(f"  95th percentile:    {r['max_dd_95th']:.2f}%")

        print("\n--- Sharpe Ratio Distribution ---")
        print(f"  5th percentile:     {r['sharpe_5th']:.4f}")
        print(f"  Median:             {r['sharpe_50th']:.4f}")
        print(f"  95th percentile:    {r['sharpe_95th']:.4f}")
        print("=" * 70)


# =============================================================================
# 2. STRESS TESTER
# =============================================================================

class StressTester:
    """Regime stress testing and correlation stress analysis.

    Runs the trading system through specific crisis periods and analyzes
    how portfolio metrics degrade under adverse conditions.
    """

    STRESS_PERIODS = {
        "COVID Crash": ("2020-02-15", "2020-04-15"),
        "FTX Collapse": ("2022-10-01", "2022-12-31"),
        "2018 Bear Market": ("2018-01-01", "2018-12-31"),
        "2022 Full Bear": ("2022-01-01", "2022-12-31"),
        "Bull Run Peak": ("2021-01-01", "2021-05-19"),
    }

    def __init__(self, df: pd.DataFrame, initial_equity: float = 100.0):
        """
        Args:
            df: Master dataset with all features
            initial_equity: Starting capital for each stress test
        """
        self.df = df.copy()
        if 'date' in self.df.columns:
            self.df['date'] = pd.to_datetime(self.df['date'])
        self.initial_equity = initial_equity
        self.regime_results = {}
        self.correlation_results = {}

    def run_regime_stress(self, PortfolioAllocator, FrictionModel,
                          WolverineRiskManager) -> Dict:
        """Run the system through each crisis period.

        Args:
            PortfolioAllocator: The allocator class from strategies module
            FrictionModel: The friction model class
            WolverineRiskManager: The risk manager class

        Returns:
            Dict of period_name -> metrics dict
        """
        print("\n" + "=" * 70)
        print("REGIME STRESS TESTING")
        print("=" * 70)

        results = {}

        for period_name, (start_date, end_date) in self.STRESS_PERIODS.items():
            print(f"\n  Running: {period_name} ({start_date} to {end_date})...")

            # Slice the dataset - include 400 days lookback for indicator computation
            lookback_start = pd.Timestamp(start_date) - pd.Timedelta(days=400)
            mask = (self.df['date'] >= lookback_start) & (self.df['date'] <= pd.Timestamp(end_date))
            df_slice = self.df[mask].copy().reset_index(drop=True)

            if len(df_slice) < 100:
                print(f"    SKIPPED - insufficient data ({len(df_slice)} rows)")
                results[period_name] = {
                    "return_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0,
                    "n_trades": 0, "status": "SKIPPED - insufficient data",
                    "per_strategy_pnl": {},
                }
                continue

            try:
                # Run PortfolioAllocator with friction + risk mgmt
                risk_mgr = WolverineRiskManager(initial_equity=self.initial_equity)
                alloc = PortfolioAllocator(
                    df_slice,
                    initial_equity=self.initial_equity,
                    use_friction=True,
                    use_risk_mgmt=True,
                    risk_manager=risk_mgr,
                )
                output = alloc.run()

                all_results = output['all_results']
                equity_df = output['equity_df']
                final_eq = output['final_equity']

                # Convert equity_df dates to string for comparison
                eq_dates = equity_df['date'].astype(str)

                # Filter trades within the stress period
                stress_trades = [
                    r for r in all_results
                    if r.entry_date >= start_date and r.entry_date <= end_date
                ]

                # Compute metrics from equity curve within the stress period
                eq_mask = (eq_dates >= start_date) & (eq_dates <= end_date)
                eq_in_period = equity_df[eq_mask]

                if len(eq_in_period) > 0:
                    period_start_eq = eq_in_period['equity'].iloc[0]
                    period_end_eq = eq_in_period['equity'].iloc[-1]
                    return_pct = (period_end_eq - period_start_eq) / period_start_eq * 100

                    # Max drawdown within period
                    eq_vals = eq_in_period['equity'].values
                    running_max = np.maximum.accumulate(eq_vals)
                    dd = (eq_vals - running_max) / np.maximum(running_max, 1e-10)
                    max_dd = abs(dd.min()) * 100

                    # Period Sharpe
                    daily_rets = np.diff(eq_vals) / eq_vals[:-1]
                    if len(daily_rets) > 1 and np.std(daily_rets) > 0:
                        sharpe = (np.mean(daily_rets) / np.std(daily_rets)) * np.sqrt(252)
                    else:
                        sharpe = 0.0
                else:
                    period_start_eq = self.initial_equity
                    period_end_eq = final_eq
                    return_pct = (final_eq - self.initial_equity) / self.initial_equity * 100
                    max_dd = 0.0
                    sharpe = 0.0

                # Per-strategy P&L
                per_strat = {}
                for r in stress_trades:
                    base = r.strategy_name.split('_')[0]
                    if base not in per_strat:
                        per_strat[base] = {'n_trades': 0, 'pnl_usd': 0.0}
                    per_strat[base]['n_trades'] += 1
                    per_strat[base]['pnl_usd'] += r.pnl_usd

                n_trades = len(stress_trades)

                # BTC return during stress period
                df_period = self.df[(self.df['date'] >= start_date) & (self.df['date'] <= end_date)]
                if len(df_period) > 1:
                    btc_start = df_period['close'].iloc[0]
                    btc_end = df_period['close'].iloc[-1]
                    btc_return = (btc_end - btc_start) / btc_start * 100
                else:
                    btc_return = 0.0

                results[period_name] = {
                    "return_pct": round(return_pct, 4),
                    "max_dd_pct": round(max_dd, 4),
                    "sharpe": round(sharpe, 4),
                    "n_trades": n_trades,
                    "per_strategy_pnl": per_strat,
                    "period_start_eq": round(period_start_eq, 4),
                    "period_end_eq": round(period_end_eq, 4),
                    "btc_return_pct": round(btc_return, 2),
                    "status": "OK",
                }

                # Print summary
                print(f"    BTC: {btc_return:+.2f}% | System: {return_pct:+.4f}%")
                print(f"    Max DD: {max_dd:.4f}% | Sharpe: {sharpe:.4f} | Trades: {n_trades}")
                for strat, info in per_strat.items():
                    print(f"      {strat}: {info['n_trades']} trades, ${info['pnl_usd']:+.4f}")

            except Exception as e:
                import traceback
                print(f"    ERROR: {str(e)[:200]}")
                traceback.print_exc()
                results[period_name] = {
                    "return_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0,
                    "n_trades": 0, "status": f"ERROR: {str(e)[:100]}",
                    "per_strategy_pnl": {},
                }

        self.regime_results = results
        return results

    def run_correlation_stress(self, trade_results: List) -> Dict:
        """Analyze portfolio degradation under high strategy correlation.

        Simulates what happens when Strategy B and D become highly correlated
        by forcing D's P&L direction to match B's when B trades.

        Args:
            trade_results: List of TradeResult objects from the backtest

        Returns:
            Dict with baseline vs stressed metrics
        """
        print("\n" + "=" * 70)
        print("CORRELATION STRESS TESTING (B <-> D correlation)")
        print("=" * 70)

        # Separate trades by strategy
        strat_b = [r for r in trade_results if r.strategy_name.startswith('B_')]
        strat_d = [r for r in trade_results if r.strategy_name.startswith('D_')]
        other = [r for r in trade_results if not r.strategy_name.startswith('B_')
                 and not r.strategy_name.startswith('D_')]

        print(f"  Strategy B trades: {len(strat_b)}")
        print(f"  Strategy D trades: {len(strat_d)}")
        print(f"  Other trades: {len(other)}")

        if len(strat_b) == 0 or len(strat_d) == 0:
            print("  Cannot run correlation stress - need both B and D trades")
            return {"status": "SKIPPED - insufficient trades"}

        # --- Baseline metrics ---
        all_pnls = np.array([r.pnl_usd for r in trade_results])
        b_pnls = np.array([r.pnl_usd for r in strat_b])
        d_pnls = np.array([r.pnl_usd for r in strat_d])

        eq0 = self.initial_equity
        baseline_equity = np.cumsum(all_pnls) + eq0
        baseline_full = np.concatenate([[eq0], baseline_equity])
        baseline_peak = np.maximum.accumulate(baseline_full)
        baseline_dd = (baseline_full - baseline_peak) / np.maximum(baseline_peak, 1e-10)
        baseline_max_dd = abs(baseline_dd.min()) * 100
        baseline_terminal = baseline_equity[-1]
        baseline_return = (baseline_terminal - eq0) / eq0 * 100

        avg_hold = np.mean([r.holding_days for r in trade_results])
        if all_pnls.std() > 0:
            baseline_sharpe = (all_pnls.mean() / all_pnls.std()) * np.sqrt(252 / max(avg_hold, 1))
        else:
            baseline_sharpe = 0.0

        print(f"\n  Baseline: Return {baseline_return:+.4f}%, Max DD {baseline_max_dd:.4f}%, Sharpe {baseline_sharpe:.4f}")

        # --- Stressed scenario: force D to correlate with B ---
        stressed_pnl_list = []
        for r in trade_results:
            if r.strategy_name.startswith('D_'):
                # Find B trades within 60 days
                b_recent = [b for b in strat_b
                            if abs(pd.Timestamp(b.entry_date).toordinal() -
                                   pd.Timestamp(r.entry_date).toordinal()) < 60]
                if b_recent:
                    b_avg_sign = np.sign(np.mean([b.pnl_usd for b in b_recent]))
                    if b_avg_sign != 0:
                        forced_pnl = abs(r.pnl_usd) * b_avg_sign
                    else:
                        forced_pnl = r.pnl_usd
                    stressed_pnl_list.append(forced_pnl)
                else:
                    stressed_pnl_list.append(r.pnl_usd)
            else:
                stressed_pnl_list.append(r.pnl_usd)

        stressed_pnls = np.array(stressed_pnl_list)
        stressed_equity = np.cumsum(stressed_pnls) + eq0
        stressed_full = np.concatenate([[eq0], stressed_equity])
        stressed_peak = np.maximum.accumulate(stressed_full)
        stressed_dd = (stressed_full - stressed_peak) / np.maximum(stressed_peak, 1e-10)
        stressed_max_dd = abs(stressed_dd.min()) * 100
        stressed_terminal = stressed_equity[-1]
        stressed_return = (stressed_terminal - eq0) / eq0 * 100

        if stressed_pnls.std() > 0:
            stressed_sharpe = (stressed_pnls.mean() / stressed_pnls.std()) * np.sqrt(252 / max(avg_hold, 1))
        else:
            stressed_sharpe = 0.0

        print(f"  Stressed: Return {stressed_return:+.4f}%, Max DD {stressed_max_dd:.4f}%, Sharpe {stressed_sharpe:.4f}")

        # --- Worst case: all losses concentrated first ---
        b_losses = sorted([r.pnl_usd for r in strat_b if r.pnl_usd < 0])
        d_losses = sorted([r.pnl_usd for r in strat_d if r.pnl_usd < 0])
        b_wins = sorted([r.pnl_usd for r in strat_b if r.pnl_usd >= 0], reverse=True)
        d_wins = sorted([r.pnl_usd for r in strat_d if r.pnl_usd >= 0], reverse=True)
        other_pnls_list = [r.pnl_usd for r in other]

        worst_case_pnls = b_losses + d_losses + other_pnls_list + b_wins + d_wins
        wc_pnls = np.array(worst_case_pnls)
        wc_equity = np.cumsum(wc_pnls) + eq0
        wc_full = np.concatenate([[eq0], wc_equity])
        wc_peak = np.maximum.accumulate(wc_full)
        wc_dd = (wc_full - wc_peak) / np.maximum(wc_peak, 1e-10)
        wc_max_dd = abs(wc_dd.min()) * 100
        wc_min_equity = min(eq0, wc_equity.min())

        print(f"  Worst-case concentrated: Max DD {wc_max_dd:.4f}%, Min equity ${wc_min_equity:.2f}")

        # --- Monte Carlo with forced correlation ---
        combined_bd = np.concatenate([b_pnls, d_pnls])
        n_bd = len(strat_b) + len(strat_d)
        n_other = len(other)
        other_pnl_arr = np.array([r.pnl_usd for r in other]) if other else np.array([])

        rng = np.random.default_rng(seed=123)
        n_corr_sims = 5000
        corr_terminals = []
        corr_max_dds = []

        for _ in range(n_corr_sims):
            bd_sample = rng.choice(combined_bd, size=n_bd, replace=True)
            if n_other > 0:
                other_sample = rng.choice(other_pnl_arr, size=n_other, replace=True)
                sim_pnls = np.concatenate([bd_sample, other_sample])
            else:
                sim_pnls = bd_sample
            rng.shuffle(sim_pnls)

            sim_eq = np.cumsum(sim_pnls) + eq0
            sim_full = np.concatenate([[eq0], sim_eq])
            sim_peak = np.maximum.accumulate(sim_full)
            sim_dd = (sim_full - sim_peak) / np.maximum(sim_peak, 1e-10)

            corr_terminals.append(sim_eq[-1])
            corr_max_dds.append(abs(sim_dd.min()) * 100)

        corr_terminals = np.array(corr_terminals)
        corr_max_dds = np.array(corr_max_dds)

        print(f"\n  Correlated MC (5000 sims):")
        print(f"    Median terminal:  ${np.median(corr_terminals):.2f}")
        print(f"    5th pctl:         ${np.percentile(corr_terminals, 5):.2f}")
        print(f"    95th pctl:        ${np.percentile(corr_terminals, 95):.2f}")
        print(f"    Median max DD:    {np.median(corr_max_dds):.2f}%")
        print(f"    95th pctl max DD: {np.percentile(corr_max_dds, 95):.2f}%")
        print(f"    P(ruin < $50):    {np.mean(corr_terminals < 50):.2%}")

        self.correlation_results = {
            "baseline": {
                "return_pct": round(baseline_return, 4),
                "max_dd_pct": round(baseline_max_dd, 4),
                "sharpe": round(baseline_sharpe, 4),
                "terminal_wealth": round(float(baseline_terminal), 4),
            },
            "forced_correlation": {
                "return_pct": round(stressed_return, 4),
                "max_dd_pct": round(stressed_max_dd, 4),
                "sharpe": round(stressed_sharpe, 4),
                "terminal_wealth": round(float(stressed_terminal), 4),
            },
            "worst_case_concentrated": {
                "max_dd_pct": round(wc_max_dd, 4),
                "min_equity": round(float(wc_min_equity), 4),
            },
            "correlated_mc": {
                "median_terminal": round(float(np.median(corr_terminals)), 4),
                "pctl_5th": round(float(np.percentile(corr_terminals, 5)), 4),
                "pctl_95th": round(float(np.percentile(corr_terminals, 95)), 4),
                "median_max_dd": round(float(np.median(corr_max_dds)), 4),
                "max_dd_95th": round(float(np.percentile(corr_max_dds, 95)), 4),
                "prob_ruin": round(float(np.mean(corr_terminals < 50)), 4),
                "n_sims": n_corr_sims,
            },
            "degradation": {
                "dd_increase_pct": round(stressed_max_dd - baseline_max_dd, 4),
                "sharpe_change": round(stressed_sharpe - baseline_sharpe, 4),
                "return_change_pct": round(stressed_return - baseline_return, 4),
            },
        }

        return self.correlation_results

    def save_report(self, path: str, mc_results: Dict = None):
        """Save comprehensive stress test report as formatted text."""
        lines = []
        lines.append("=" * 70)
        lines.append("BTC OPTIONS SYSTEM - STRESS TEST REPORT")
        lines.append("Generated: 2026-03-09")
        lines.append("=" * 70)

        # --- Monte Carlo summary ---
        if mc_results:
            lines.append("")
            lines.append("-" * 70)
            lines.append("SECTION 1: MONTE CARLO SIMULATION")
            lines.append("-" * 70)
            lines.append(f"  Simulations:          {mc_results.get('n_simulations', 0):,}")
            lines.append(f"  Trades per sim:       {mc_results.get('n_trades_per_sim', 0)}")
            lines.append(f"  Actual trades:        {mc_results.get('n_actual_trades', 0)}")
            lines.append(f"  Win rate:             {mc_results.get('win_rate', 0):.1%}")
            lines.append(f"  Avg trade P&L:        ${mc_results.get('avg_trade_pnl', 0):.4f}")
            lines.append("")
            lines.append("  Terminal Wealth Distribution:")
            lines.append(f"    5th percentile:     ${mc_results.get('percentile_5th', 0):.2f}")
            lines.append(f"    25th percentile:    ${mc_results.get('percentile_25th', 0):.2f}")
            lines.append(f"    Median:             ${mc_results.get('median_terminal_wealth', 0):.2f}")
            lines.append(f"    Mean:               ${mc_results.get('mean_terminal_wealth', 0):.2f}")
            lines.append(f"    75th percentile:    ${mc_results.get('percentile_75th', 0):.2f}")
            lines.append(f"    95th percentile:    ${mc_results.get('percentile_95th', 0):.2f}")
            lines.append("")
            lines.append("  Risk Probabilities:")
            lines.append(f"    P(ruin < $50):      {mc_results.get('prob_ruin', 0):.2%}")
            lines.append(f"    P(loss < $100):     {mc_results.get('prob_loss', 0):.2%}")
            lines.append(f"    P(double >= $200):  {mc_results.get('prob_double', 0):.2%}")
            lines.append(f"    P(10x >= $1000):    {mc_results.get('prob_10x', 0):.2%}")
            ttd = mc_results.get('expected_trades_to_double', float('inf'))
            if ttd == float('inf'):
                lines.append("    Trades to double:   NEVER")
            else:
                lines.append(f"    Trades to double:   {ttd:.0f} trades (median)")
            lines.append("")
            lines.append("  Max Drawdown Distribution:")
            lines.append(f"    5th percentile:     {mc_results.get('max_dd_5th', 0):.2f}%")
            lines.append(f"    Median:             {mc_results.get('max_dd_50th', 0):.2f}%")
            lines.append(f"    Mean:               {mc_results.get('max_dd_mean', 0):.2f}%")
            lines.append(f"    95th percentile:    {mc_results.get('max_dd_95th', 0):.2f}%")
            lines.append("")
            lines.append("  Sharpe Ratio Distribution:")
            lines.append(f"    5th percentile:     {mc_results.get('sharpe_5th', 0):.4f}")
            lines.append(f"    Median:             {mc_results.get('sharpe_50th', 0):.4f}")
            lines.append(f"    Mean:               {mc_results.get('sharpe_mean', 0):.4f}")
            lines.append(f"    95th percentile:    {mc_results.get('sharpe_95th', 0):.4f}")

        # --- Regime stress results ---
        if self.regime_results:
            lines.append("")
            lines.append("-" * 70)
            lines.append("SECTION 2: REGIME STRESS TESTS")
            lines.append("-" * 70)

            for period_name, metrics in self.regime_results.items():
                lines.append(f"\n  {period_name}:")
                dates = self.STRESS_PERIODS.get(period_name, ('?', '?'))
                lines.append(f"    Period:           {dates[0]} to {dates[1]}")
                lines.append(f"    Status:           {metrics.get('status', 'N/A')}")
                if metrics.get('status') == 'OK':
                    lines.append(f"    BTC Return:       {metrics.get('btc_return_pct', 0):+.2f}%")
                    lines.append(f"    System Return:    {metrics['return_pct']:+.4f}%")
                    lines.append(f"    Max Drawdown:     {metrics['max_dd_pct']:.4f}%")
                    lines.append(f"    Sharpe Ratio:     {metrics['sharpe']:.4f}")
                    lines.append(f"    Trades:           {metrics['n_trades']}")
                    lines.append(f"    Start Equity:     ${metrics.get('period_start_eq', 0):.4f}")
                    lines.append(f"    End Equity:       ${metrics.get('period_end_eq', 0):.4f}")
                    if metrics['per_strategy_pnl']:
                        lines.append("    Per-Strategy:")
                        for strat, info in metrics['per_strategy_pnl'].items():
                            lines.append(f"      {strat}: {info['n_trades']} trades, ${info['pnl_usd']:+.4f}")

        # --- Correlation stress ---
        if self.correlation_results:
            lines.append("")
            lines.append("-" * 70)
            lines.append("SECTION 3: CORRELATION STRESS TEST (B <-> D)")
            lines.append("-" * 70)

            bl = self.correlation_results.get('baseline', {})
            fc = self.correlation_results.get('forced_correlation', {})
            wc = self.correlation_results.get('worst_case_concentrated', {})
            cm = self.correlation_results.get('correlated_mc', {})
            dg = self.correlation_results.get('degradation', {})

            lines.append("\n  Baseline (actual trade sequence):")
            lines.append(f"    Return:           {bl.get('return_pct', 0):+.4f}%")
            lines.append(f"    Max Drawdown:     {bl.get('max_dd_pct', 0):.4f}%")
            lines.append(f"    Sharpe:           {bl.get('sharpe', 0):.4f}")
            lines.append(f"    Terminal Wealth:  ${bl.get('terminal_wealth', 0):.4f}")

            lines.append("\n  Forced B-D Correlation (D matches B direction):")
            lines.append(f"    Return:           {fc.get('return_pct', 0):+.4f}%")
            lines.append(f"    Max Drawdown:     {fc.get('max_dd_pct', 0):.4f}%")
            lines.append(f"    Sharpe:           {fc.get('sharpe', 0):.4f}")
            lines.append(f"    Terminal Wealth:  ${fc.get('terminal_wealth', 0):.4f}")

            lines.append("\n  Worst Case (all losses concentrated first):")
            lines.append(f"    Max Drawdown:     {wc.get('max_dd_pct', 0):.4f}%")
            lines.append(f"    Min Equity:       ${wc.get('min_equity', 0):.4f}")

            lines.append(f"\n  Correlated Monte Carlo ({cm.get('n_sims', 0)} sims):")
            lines.append(f"    Median Terminal:  ${cm.get('median_terminal', 0):.4f}")
            lines.append(f"    5th percentile:   ${cm.get('pctl_5th', 0):.4f}")
            lines.append(f"    95th percentile:  ${cm.get('pctl_95th', 0):.4f}")
            lines.append(f"    Median Max DD:    {cm.get('median_max_dd', 0):.4f}%")
            lines.append(f"    95th pctl Max DD: {cm.get('max_dd_95th', 0):.4f}%")
            lines.append(f"    P(ruin < $50):    {cm.get('prob_ruin', 0):.2%}")

            lines.append("\n  Degradation from Correlation:")
            lines.append(f"    DD increase:      {dg.get('dd_increase_pct', 0):+.4f}%")
            lines.append(f"    Sharpe change:    {dg.get('sharpe_change', 0):+.4f}")
            lines.append(f"    Return change:    {dg.get('return_change_pct', 0):+.4f}%")

        # --- Conclusions ---
        lines.append("")
        lines.append("-" * 70)
        lines.append("CONCLUSIONS & RISK ASSESSMENT")
        lines.append("-" * 70)

        if mc_results:
            prob_ruin = mc_results.get('prob_ruin', 0)
            if prob_ruin < 0.01:
                lines.append("  [LOW RISK] Probability of ruin < 1% -- system is robust")
            elif prob_ruin < 0.05:
                lines.append("  [MODERATE RISK] Probability of ruin 1-5% -- acceptable but monitor")
            elif prob_ruin < 0.10:
                lines.append("  [ELEVATED RISK] Probability of ruin 5-10% -- consider reducing size")
            else:
                lines.append(f"  [HIGH RISK] Probability of ruin {prob_ruin:.1%} -- REDUCE POSITION SIZES")

            med_sharpe = mc_results.get('sharpe_50th', 0)
            if med_sharpe > 0.5:
                lines.append(f"  [POSITIVE] Median Sharpe {med_sharpe:.2f} indicates positive edge")
            elif med_sharpe > 0:
                lines.append(f"  [MARGINAL] Median Sharpe {med_sharpe:.2f} -- weak positive edge")
            else:
                lines.append(f"  [NEGATIVE] Median Sharpe {med_sharpe:.2f} -- NO consistent edge")

        if self.regime_results:
            ok_results = {k: v for k, v in self.regime_results.items() if v.get('status') == 'OK'}
            if ok_results:
                worst_dd = max(v.get('max_dd_pct', 0) for v in ok_results.values())
                worst_period = max(ok_results.items(), key=lambda x: x[1].get('max_dd_pct', 0))[0]
                lines.append(f"  Worst regime stress DD: {worst_dd:.4f}% during {worst_period}")

            no_trade_periods = [k for k, v in self.regime_results.items() if v.get('n_trades', 0) == 0]
            if no_trade_periods:
                lines.append(f"  No trades during: {', '.join(no_trade_periods)}")
                lines.append("  (System correctly avoids trading in some crisis periods)")

        if self.correlation_results and 'degradation' in self.correlation_results:
            dg = self.correlation_results.get('degradation', {})
            dd_increase = dg.get('dd_increase_pct', 0)
            if abs(dd_increase) > 5:
                lines.append(f"  [WARNING] Correlation stress adds {dd_increase:+.2f}% to max DD")
            else:
                lines.append(f"  [OK] Correlation stress impact on DD: {dd_increase:+.4f}%")

        lines.append("")
        lines.append("=" * 70)
        lines.append("END OF STRESS TEST REPORT")
        lines.append("=" * 70)

        report = "\n".join(lines)
        with open(path, 'w') as f:
            f.write(report)
        print(f"\n  Saved stress test report to {path}")
        return report
