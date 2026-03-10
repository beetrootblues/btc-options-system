#!/usr/bin/env python3
"""
Walk-Forward Optimization (WFO) for the BTC Options Trading System.

Tests parameter robustness by:
  1. Splitting data into rolling in-sample/out-of-sample windows
  2. Grid-searching key parameters on in-sample data
  3. Applying best IS parameters to out-of-sample period
  4. Stitching all OOS equity curves together
  5. Computing WFO efficiency ratio (OOS Sharpe / IS Sharpe)

If WFO Sharpe << full-backtest Sharpe, the system is overfit.

Author: Code Agent
Date: 2026-03-09
"""

import sys
import os
import json
import time
import warnings
import itertools
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Import from the existing strategies module
sys.path.insert(0, "/home/user/files")
from btc_options_system.strategies import (
    StrategyA_VolSelling,
    StrategyB_MomentumBreakout,
    StrategyC_EventVol,
    StrategyD_MeanReversion,
    PortfolioAllocator,
    FrictionModel,
    WolverineRiskManager,
    compute_stats,
    TradeResult,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

# Walk-forward window sizes (days)
IS_WINDOW = 365       # 12 months in-sample
OOS_WINDOW = 91       # 3 months out-of-sample
ROLL_STEP = 182       # Roll forward 6 months

# Parameter grids -- only the most impactful params per strategy
PARAM_GRID = {
    "B": {
        "class": StrategyB_MomentumBreakout,
        "params": {
            "BASE_RISK_PCT": [0.02, 0.04],
            "TAKE_PROFIT_MULT": [2.0, 3.0],
            "STOP_LOSS_FLOOR": [0.25, 0.50],
        },
    },
    "C": {
        "class": StrategyC_EventVol,
        "params": {
            "PRE_RISK_PCT": [0.02, 0.04],
            "POST_TP_MULT": [1.5, 3.0],
            "POST_SL_FLOOR": [0.20, 0.40],
        },
    },
    "D": {
        "class": StrategyD_MeanReversion,
        "params": {
            "LONG_RISK_PCT": [0.015, 0.03],
            "LONG_TP_MULT": [2.0, 3.5],
            "LONG_SL_FLOOR": [0.20, 0.40],
        },
    },
}

# Default (original) parameter values for restoring after each run
DEFAULT_PARAMS = {
    "B": {
        "BASE_RISK_PCT": 0.02,
        "HIGH_CONF_RISK_PCT": 0.03,
        "TAKE_PROFIT_MULT": 3.0,
        "STOP_LOSS_FLOOR": 0.20,
    },
    "C": {
        "PRE_RISK_PCT": 0.03,
        "POST_TP_MULT": 2.0,
        "POST_SL_FLOOR": 0.30,
    },
    "D": {
        "LONG_RISK_PCT": 0.02,
        "LONG_TP_MULT": 2.50,
        "LONG_SL_FLOOR": 0.30,
    },
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def compute_sharpe_from_equity(equity_series: pd.Series, periods_per_year: float = 365.0) -> float:
    """Compute annualized Sharpe ratio from a daily equity series."""
    if len(equity_series) < 2:
        return 0.0
    daily_returns = equity_series.pct_change().dropna()
    if len(daily_returns) < 2 or daily_returns.std() == 0:
        return 0.0
    return float(daily_returns.mean() / daily_returns.std() * np.sqrt(periods_per_year))


def compute_max_dd(equity_series: pd.Series) -> float:
    """Compute max drawdown percentage from equity series."""
    if len(equity_series) < 2:
        return 0.0
    peak = equity_series.cummax()
    dd = (peak - equity_series) / peak
    return float(dd.max() * 100)


def restore_defaults():
    """Restore all strategy class constants to their original defaults."""
    for param, val in DEFAULT_PARAMS["B"].items():
        setattr(StrategyB_MomentumBreakout, param, val)
    for param, val in DEFAULT_PARAMS["C"].items():
        setattr(StrategyC_EventVol, param, val)
    for param, val in DEFAULT_PARAMS["D"].items():
        setattr(StrategyD_MeanReversion, param, val)


def apply_params(strategy_letter: str, params: Dict[str, Any]):
    """Apply parameter values to a strategy class via monkey-patching."""
    cls = PARAM_GRID[strategy_letter]["class"]
    for param_name, param_val in params.items():
        setattr(cls, param_name, param_val)


def run_allocator_on_slice(
    df_slice: pd.DataFrame,
    equity: float,
    use_friction: bool = True,
    use_risk_mgmt: bool = False,
) -> Dict:
    """
    Run PortfolioAllocator on a data slice.
    
    Returns dict with: final_equity, sharpe, total_return_pct, 
    max_dd_pct, n_trades, all_results, equity_series
    """
    if len(df_slice) < 30:
        return {
            "final_equity": equity,
            "sharpe": 0.0,
            "total_return_pct": 0.0,
            "max_dd_pct": 0.0,
            "n_trades": 0,
            "all_results": [],
            "equity_series": pd.Series([equity]),
        }

    allocator = PortfolioAllocator(
        df_slice,
        initial_equity=equity,
        use_friction=use_friction,
        use_risk_mgmt=use_risk_mgmt,
    )
    result = allocator.run()

    eq_series = result["equity_df"]["equity"]
    sharpe = compute_sharpe_from_equity(eq_series)
    final_eq = result["final_equity"]
    total_ret = (final_eq - equity) / equity * 100 if equity > 0 else 0.0
    max_dd = compute_max_dd(eq_series)
    n_trades = len(result["all_results"])

    return {
        "final_equity": final_eq,
        "sharpe": sharpe,
        "total_return_pct": total_ret,
        "max_dd_pct": max_dd,
        "n_trades": n_trades,
        "all_results": result["all_results"],
        "equity_series": eq_series,
    }


# =============================================================================
# WALK-FORWARD OPTIMIZER
# =============================================================================

class WalkForwardOptimizer:
    """Walk-Forward Optimization engine for the BTC options system.
    
    For each rolling window:
      1. Grid-search strategy parameters on in-sample data
      2. Apply best IS parameters to out-of-sample period
      3. Record OOS performance
    
    Optimizes one strategy at a time (not joint) to keep the search
    space manageable.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        initial_equity: float = 100.0,
        is_window: int = IS_WINDOW,
        oos_window: int = OOS_WINDOW,
        roll_step: int = ROLL_STEP,
    ):
        self.df = df.copy()
        self.initial_equity = initial_equity
        self.is_window = is_window
        self.oos_window = oos_window
        self.roll_step = roll_step

        # Ensure date column is string for slicing
        if "date" in self.df.columns:
            self.df["date"] = self.df["date"].astype(str)

        self.dates = pd.to_datetime(self.df["date"])
        self.min_date = self.dates.min()
        self.max_date = self.dates.max()

    def _generate_windows(self) -> List[Tuple[int, pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
        """Generate (window_num, is_start, is_end, oos_start, oos_end) tuples."""
        windows = []
        window_num = 1
        is_start = self.min_date

        while True:
            is_end = is_start + timedelta(days=self.is_window - 1)
            oos_start = is_end + timedelta(days=1)
            oos_end = oos_start + timedelta(days=self.oos_window - 1)

            # Stop if OOS end exceeds data
            if oos_end > self.max_date:
                # Allow partial OOS window if at least 30 days
                if oos_start + timedelta(days=30) <= self.max_date:
                    oos_end = self.max_date
                else:
                    break

            windows.append((window_num, is_start, is_end, oos_start, oos_end))
            window_num += 1
            is_start += timedelta(days=self.roll_step)

        return windows

    def _get_slice(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Get data slice between dates (inclusive)."""
        mask = (self.dates >= start) & (self.dates <= end)
        return self.df.loc[mask].reset_index(drop=True)

    def _grid_search_strategy(
        self,
        strategy_letter: str,
        df_is: pd.DataFrame,
        equity: float,
    ) -> Tuple[Dict[str, Any], float]:
        """Grid-search one strategy's parameters on IS data.
        
        Returns (best_params, best_sharpe).
        Restores defaults for OTHER strategies; varies only the target strategy.
        """
        grid_cfg = PARAM_GRID[strategy_letter]
        param_names = list(grid_cfg["params"].keys())
        param_values = list(grid_cfg["params"].values())
        combos = list(itertools.product(*param_values))

        best_sharpe = -999.0
        best_params = {name: vals[0] for name, vals in zip(param_names, param_values)}

        for combo in combos:
            # Restore all defaults first
            restore_defaults()
            # Apply this combo
            params = dict(zip(param_names, combo))
            apply_params(strategy_letter, params)

            # Run allocator (friction=True, risk_mgmt=False for speed)
            result = run_allocator_on_slice(df_is, equity, use_friction=True, use_risk_mgmt=False)
            sharpe = result["sharpe"]

            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_params = params.copy()

        return best_params, best_sharpe

    def _optimize_all_strategies(
        self,
        df_is: pd.DataFrame,
        equity: float,
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float]]:
        """Optimize each strategy independently on IS data.
        
        Returns (all_best_params, all_is_sharpes).
        """
        all_best = {}
        all_sharpes = {}

        for letter in ["B", "C", "D"]:
            best_params, best_sharpe = self._grid_search_strategy(letter, df_is, equity)
            all_best[letter] = best_params
            all_sharpes[letter] = best_sharpe

        return all_best, all_sharpes

    def run(self, verbose: bool = True) -> Dict:
        """Execute full walk-forward optimization.
        
        Returns dict with:
          - window_results: list of per-window results
          - parameter_stability: DataFrame of optimal params per window
          - summary: overall WFO metrics
        """
        windows = self._generate_windows()
        n_windows = len(windows)

        if verbose:
            print(f"\n{'='*70}")
            print(f"  WALK-FORWARD OPTIMIZATION")
            print(f"  IS={self.is_window}d, OOS={self.oos_window}d, Roll={self.roll_step}d")
            print(f"  Windows: {n_windows}")
            print(f"  Date range: {self.min_date.date()} to {self.max_date.date()}")
            print(f"  Strategies optimized: B, C, D")
            print(f"  Grid size per strategy: 8 combos")
            print(f"{'='*70}\n")

        window_results = []
        param_stability_rows = []
        oos_equity_segments = []  # (dates, equity) for stitching
        running_equity = self.initial_equity
        total_start = time.time()

        for win_num, is_start, is_end, oos_start, oos_end in windows:
            win_start_time = time.time()

            # Get data slices
            df_is = self._get_slice(is_start, is_end)
            df_oos = self._get_slice(oos_start, oos_end)

            if len(df_is) < 60 or len(df_oos) < 15:
                if verbose:
                    print(f"  Window {win_num}/{n_windows}: SKIPPED (insufficient data: IS={len(df_is)}, OOS={len(df_oos)})")
                continue

            # --- Phase 1: Grid search on IS ---
            all_best_params, all_is_sharpes = self._optimize_all_strategies(df_is, running_equity)

            # Compute combined IS Sharpe with best params applied
            restore_defaults()
            for letter, params in all_best_params.items():
                apply_params(letter, params)
            is_result = run_allocator_on_slice(df_is, running_equity, use_friction=True, use_risk_mgmt=False)
            is_sharpe_combined = is_result["sharpe"]

            # --- Phase 2: Apply best params to OOS ---
            # Params are already set from the combined IS run above
            oos_result = run_allocator_on_slice(df_oos, running_equity, use_friction=True, use_risk_mgmt=False)
            oos_sharpe = oos_result["sharpe"]
            oos_return = oos_result["total_return_pct"]
            oos_max_dd = oos_result["max_dd_pct"]
            oos_trades = oos_result["n_trades"]

            # Record OOS equity segment
            oos_eq = oos_result["equity_series"]
            oos_dates_slice = self._get_slice(oos_start, oos_end)
            if len(oos_dates_slice) > 0 and len(oos_eq) > 0:
                oos_equity_segments.append({
                    "dates": oos_dates_slice["date"].values[:len(oos_eq)],
                    "equity": oos_eq.values,
                })

            # Update running equity for next window
            running_equity = oos_result["final_equity"]

            # Serialize best params
            params_json = json.dumps({
                f"{letter}_{k}": v
                for letter, params in all_best_params.items()
                for k, v in params.items()
            })

            # Record results
            win_result = {
                "window_num": win_num,
                "is_start": str(is_start.date()),
                "is_end": str(is_end.date()),
                "oos_start": str(oos_start.date()),
                "oos_end": str(oos_end.date()),
                "is_sharpe": round(is_sharpe_combined, 4),
                "oos_sharpe": round(oos_sharpe, 4),
                "oos_return_pct": round(oos_return, 4),
                "best_params": params_json,
                "oos_trades": oos_trades,
                "oos_max_dd": round(oos_max_dd, 4),
                "is_days": len(df_is),
                "oos_days": len(df_oos),
                "oos_final_equity": round(running_equity, 4),
            }
            window_results.append(win_result)

            # Record parameter stability
            for letter, params in all_best_params.items():
                for pname, pval in params.items():
                    param_stability_rows.append({
                        "window_num": win_num,
                        "strategy": letter,
                        "param_name": pname,
                        "optimal_value": pval,
                    })

            win_elapsed = time.time() - win_start_time
            if verbose:
                print(
                    f"  Window {win_num:>2d}/{n_windows}: "
                    f"IS={str(is_start.date())} to {str(is_end.date())} | "
                    f"OOS={str(oos_start.date())} to {str(oos_end.date())} | "
                    f"IS Sharpe={is_sharpe_combined:>6.2f} | "
                    f"OOS Sharpe={oos_sharpe:>6.2f} | "
                    f"OOS Ret={oos_return:>6.2f}% | "
                    f"Trades={oos_trades:>2d} | "
                    f"{win_elapsed:.1f}s"
                )

        # Restore defaults after all runs
        restore_defaults()

        total_elapsed = time.time() - total_start

        # --- Build summary ---
        results_df = pd.DataFrame(window_results)
        stability_df = pd.DataFrame(param_stability_rows)

        # Compute WFO metrics
        if len(results_df) > 0:
            mean_is_sharpe = results_df["is_sharpe"].mean()
            mean_oos_sharpe = results_df["oos_sharpe"].mean()
            median_oos_sharpe = results_df["oos_sharpe"].median()
            wfo_efficiency = (
                mean_oos_sharpe / mean_is_sharpe
                if abs(mean_is_sharpe) > 0.001
                else 0.0
            )
            oos_total_return = (
                (running_equity - self.initial_equity) / self.initial_equity * 100
            )
            pct_positive_oos = (
                (results_df["oos_sharpe"] > 0).sum() / len(results_df) * 100
            )
            total_oos_trades = results_df["oos_trades"].sum()
            avg_oos_dd = results_df["oos_max_dd"].mean()
        else:
            mean_is_sharpe = mean_oos_sharpe = median_oos_sharpe = 0.0
            wfo_efficiency = 0.0
            oos_total_return = 0.0
            pct_positive_oos = 0.0
            total_oos_trades = 0
            avg_oos_dd = 0.0

        # Parameter stability scores (lower coefficient of variation = more stable)
        param_stability_scores = {}
        if len(stability_df) > 0:
            for (strat, pname), grp in stability_df.groupby(["strategy", "param_name"]):
                vals = grp["optimal_value"].values
                mean_val = np.mean(vals)
                std_val = np.std(vals)
                cv = std_val / abs(mean_val) if abs(mean_val) > 1e-10 else 0.0
                param_stability_scores[f"{strat}_{pname}"] = {
                    "mean": round(mean_val, 6),
                    "std": round(std_val, 6),
                    "cv": round(cv, 4),
                    "stable": cv < 0.30,  # CV < 30% = stable
                    "values": vals.tolist(),
                }

        # Stitch OOS equity curve
        stitched_dates = []
        stitched_equity = []
        for seg in oos_equity_segments:
            for d, e in zip(seg["dates"], seg["equity"]):
                stitched_dates.append(d)
                stitched_equity.append(e)

        summary = {
            "n_windows": len(results_df),
            "mean_is_sharpe": round(mean_is_sharpe, 4),
            "mean_oos_sharpe": round(mean_oos_sharpe, 4),
            "median_oos_sharpe": round(median_oos_sharpe, 4),
            "wfo_efficiency_ratio": round(wfo_efficiency, 4),
            "oos_total_return_pct": round(oos_total_return, 4),
            "oos_final_equity": round(running_equity, 4),
            "pct_positive_oos_windows": round(pct_positive_oos, 1),
            "total_oos_trades": int(total_oos_trades),
            "avg_oos_max_dd": round(avg_oos_dd, 2),
            "param_stability_scores": param_stability_scores,
            "elapsed_seconds": round(total_elapsed, 1),
        }

        return {
            "window_results": results_df,
            "parameter_stability": stability_df,
            "summary": summary,
            "stitched_oos_equity": pd.DataFrame({
                "date": stitched_dates,
                "equity": stitched_equity,
            }),
        }


def print_summary(summary: Dict, results_df: pd.DataFrame, stability_df: pd.DataFrame):
    """Print formatted WFO summary report."""
    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD OPTIMIZATION RESULTS")
    print(f"{'='*70}")

    print(f"\n  --- Overall Metrics ---")
    print(f"  Windows completed:        {summary['n_windows']}")
    print(f"  Mean IS Sharpe:           {summary['mean_is_sharpe']:.4f}")
    print(f"  Mean OOS Sharpe:          {summary['mean_oos_sharpe']:.4f}")
    print(f"  Median OOS Sharpe:        {summary['median_oos_sharpe']:.4f}")
    print(f"  WFO Efficiency Ratio:     {summary['wfo_efficiency_ratio']:.4f}")
    eff = summary['wfo_efficiency_ratio']
    if eff > 0.5:
        verdict = "PASS -- system is NOT overfit"
    elif eff > 0.25:
        verdict = "MARGINAL -- some overfitting present"
    elif eff > 0:
        verdict = "WEAK -- significant overfitting"
    else:
        verdict = "FAIL -- system is overfit or OOS performance is negative"
    print(f"  Efficiency Verdict:       {verdict}")
    print(f"")
    print(f"  OOS Total Return:         {summary['oos_total_return_pct']:.2f}%")
    print(f"  OOS Final Equity:         ${summary['oos_final_equity']:.2f}")
    print(f"  % Windows with OOS > 0:   {summary['pct_positive_oos_windows']:.1f}%")
    print(f"  Total OOS Trades:         {summary['total_oos_trades']}")
    print(f"  Avg OOS Max Drawdown:     {summary['avg_oos_max_dd']:.2f}%")
    print(f"  Total Elapsed:            {summary['elapsed_seconds']:.1f}s")

    print(f"\n  --- Parameter Stability ---")
    print(f"  {'Parameter':<30s} {'Mean':>10s} {'Std':>10s} {'CV':>8s} {'Stable?':>8s}")
    print(f"  {'-'*66}")
    for pname, scores in summary["param_stability_scores"].items():
        stable_str = "YES" if scores["stable"] else "NO"
        print(
            f"  {pname:<30s} {scores['mean']:>10.4f} {scores['std']:>10.4f} "
            f"{scores['cv']:>8.4f} {stable_str:>8s}"
        )

    n_stable = sum(1 for s in summary["param_stability_scores"].values() if s["stable"])
    n_total = len(summary["param_stability_scores"])
    print(f"\n  Stable params: {n_stable}/{n_total} ({n_stable/max(n_total,1)*100:.0f}%)")

    # Per-window summary table
    if len(results_df) > 0:
        print(f"\n  --- Per-Window Summary ---")
        print(
            f"  {'Win':>4s} {'IS Period':>24s} {'OOS Period':>24s} "
            f"{'IS_Sh':>7s} {'OOS_Sh':>7s} {'OOS_Ret%':>9s} {'Trades':>7s} {'OOS_DD%':>8s}"
        )
        print(f"  {'-'*92}")
        for _, row in results_df.iterrows():
            print(
                f"  {int(row['window_num']):>4d} "
                f"{row['is_start']:>12s}-{row['is_end']:>11s} "
                f"{row['oos_start']:>12s}-{row['oos_end']:>11s} "
                f"{row['is_sharpe']:>7.2f} {row['oos_sharpe']:>7.2f} "
                f"{row['oos_return_pct']:>9.2f} {int(row['oos_trades']):>7d} "
                f"{row['oos_max_dd']:>8.2f}"
            )

    print(f"\n{'='*70}")


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """Run the full walk-forward optimization."""
    data_path = "/home/user/files/btc_options_system/btc_master_dataset.csv"
    output_dir = "/home/user/files/btc_options_system"

    print("Loading master dataset...")
    df = pd.read_csv(data_path)
    print(f"  Loaded {len(df)} rows, {df.columns.size} columns")
    print(f"  Date range: {df['date'].iloc[0]} to {df['date'].iloc[-1]}")

    # Initialize optimizer
    wfo = WalkForwardOptimizer(df, initial_equity=100.0)
    windows = wfo._generate_windows()
    print(f"  Generated {len(windows)} walk-forward windows")

    # Run WFO
    output = wfo.run(verbose=True)

    results_df = output["window_results"]
    stability_df = output["parameter_stability"]
    summary = output["summary"]
    stitched_eq = output["stitched_oos_equity"]

    # Print summary
    print_summary(summary, results_df, stability_df)

    # Save outputs
    results_path = os.path.join(output_dir, "btc_walkforward_results.csv")
    stability_path = os.path.join(output_dir, "btc_parameter_stability.csv")

    results_df.to_csv(results_path, index=False)
    print(f"\n  Saved: {results_path}")

    stability_df.to_csv(stability_path, index=False)
    print(f"  Saved: {stability_path}")

    if len(stitched_eq) > 0:
        eq_path = os.path.join(output_dir, "btc_walkforward_equity.csv")
        stitched_eq.to_csv(eq_path, index=False)
        print(f"  Saved: {eq_path}")

    print("\nWalk-Forward Optimization complete.")
    return output


if __name__ == "__main__":
    main()
