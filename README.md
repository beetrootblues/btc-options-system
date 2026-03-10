# BTC Options Quantitative Trading System

A quantitative Bitcoin options trading system with 6 decorrelated strategies, confidence-weighted position sizing, and live execution on Deribit. Built on 11.5 years of backtesting data (2014-2026).

## Performance Summary

| Metric | Backtest (11.5yr) | Live (19 scans) |
|--------|-------------------|------------------|
| Total Return | +13.90% | +0.44 BTC (~$3,000) |
| CAGR | 1.14% | N/A (< 1 day) |
| Sharpe Ratio | 0.24 | Est. 0.97 (Monte Carlo median) |
| Max Drawdown | 11.86% | 0.01% |
| Win Rate | 52.8% | 88.1% fill rate |
| Total Trades | 106 | 42 orders placed |

## System Architecture

```
Signal Engine (v2.0)
├── Market Data Pipeline (Deribit + CoinGecko APIs)
│   └── BTC price, OHLCV, DVOL, historical data
├── Indicator Engine
│   └── RV, IV, VRP, VoV, RSI, SMA, ATR, regime classification
├── 6 Strategy Scanners
│   ├── A: Mean Reversion (vol selling in MEDIUM regime)
│   ├── B: Momentum Breakout (long gamma on breakouts)
│   ├── C: Event Vol (post-event straddle buying)
│   ├── D: Iron Condor (CRISIS regime premium harvesting)
│   ├── E: Short Strangle (range-bound premium collection)
│   └── F: Jade Lizard / BWB (skew-based structures)
├── ConfidenceScorer (6-factor scoring, strategy-specific weights)
│   ├── IV/RV Environment (0-100)
│   ├── VRP Edge (0-100)
│   ├── Regime Alignment (0-100)
│   ├── Technical Confluence (0-100)
│   ├── VoV Confirmation (0-100)
│   └── Theta Profile (0-100)
├── Position Sizer (confidence-weighted, piecewise-linear interpolation)
│   └── Score 40→0.5 BTC | 60→1.5 BTC | 80→3.0 BTC | 100→5.0 BTC
└── Execution Engine (dual-mode: Deribit testnet + paper trading)
```

## Key Research Findings

1. **BTC's VRP is NEGATIVE 78% of days** -- options are systematically underpriced vs realized vol
2. **Fat tails with Hill alpha = -0.96** -- Black-Scholes catastrophically misprices deep OTM options
3. **Vol regimes are 92-96% daily persistent** -- IGARCH behavior means vol shocks are permanent
4. **Halvings produce -26% vol crush over 30 days** -- repeatable, tradeable edge
5. **Short-dated deep OTM puts are 2-3x underpriced** by BS model

## Strategies

### A: Mean Reversion Short Vol
Sells vol when RV/IV spread compresses in MEDIUM regime. 51 trades, 64.7% win rate, Sharpe 1.40.

### B: Momentum Breakout Long Gamma
Buys gamma on breakout signals above SMA with RSI confirmation. 17 trades, 64.7% win rate, Sharpe 1.13.

### C: Event Vol Post-Event Straddle
Buys straddles after regime transitions to capture vol recovery. 38 trades, 31.6% win rate but 3.32x W/L ratio, Sharpe 0.67.

### D: Iron Condor (CRISIS)
Premium harvesting in extreme vol environments. Margin-killed on $100 backtest capital but designed for larger portfolios.

### E: Short Strangle
Range-bound premium collection with confidence-weighted sizing. Active in MEDIUM regime.

### F: Jade Lizard / Broken Wing Butterfly
Skew-based structures exploiting put/call skew differentials.

## Confidence Scoring System

Each signal is scored 0-100 across 6 factors with strategy-specific weight profiles:

| Factor | Description | Iron Condor | Straddle | Strangle | Jade Lizard |
|--------|-------------|-------------|----------|----------|-------------|
| IV/RV Environment | Current vol regime alignment | 20% | 15% | 20% | 15% |
| VRP Edge | Variance risk premium strength | 25% | 30% | 25% | 20% |
| Regime Alignment | Strategy-regime fit | 20% | 15% | 20% | 25% |
| Technical Confluence | RSI/SMA/momentum signals | 10% | 15% | 10% | 15% |
| VoV Confirmation | Vol-of-vol trend alignment | 10% | 15% | 10% | 10% |
| Theta Profile | Time decay characteristics | 15% | 10% | 15% | 15% |

Signals scoring below 40 are filtered out. Position size scales linearly within confidence tiers.

## Backtesting

- **11.5 years** of daily BTC data (Sep 2014 - Mar 2026)
- **91-feature master dataset** with OHLCV, 24 volatility metrics, regime classifications, technical indicators
- **3-tier backtest**: Frictionless → +Friction (fees/spread/slippage) → +Risk Management
- **Walk-forward optimization**: 21 rolling windows, 6-month IS / 3-month OOS
- **Monte Carlo**: 10,000 simulations, median terminal wealth $131.32, 0% ruin probability

## Live Performance (19 Hourly Scans)

- **Testnet Portfolio**: 100.0 → 100.44 BTC balance
- **42 orders placed** across 3 expiry cycles (20MAR26, 27MAR26, 24APR26)
- **12 active positions**, 37 resting orders
- **Net theta**: +$932.77/day
- **Confidence range**: 0.507 to 0.843
- **Strategy mix**: Iron Condor (highest confidence 84.3%), Short Straddle, Jade Lizard, BWB, Short Strangle

## Folder Structure

```
btc-options-system/
├── src/                          # Core source code
│   ├── signal_engine.py          # Live market scanner (28KB)
│   ├── monitor.py                # Hourly orchestrator + Telegram formatter
│   ├── execution_engine.py       # Deribit testnet + paper trade execution
│   ├── strategies.py             # 6 strategy implementations (116KB)
│   ├── monte_carlo.py            # Monte Carlo simulation engine
│   ├── walk_forward.py           # Walk-forward optimization
│   └── __init__.py
├── data/                         # Backtesting datasets
│   ├── btc_master_dataset.csv    # 4,191 rows x 91 features (3.4MB)
│   ├── btc_daily_ohlcv.csv       # Daily OHLCV (2014-2026)
│   ├── btc_volatility_metrics.csv # 24 volatility metrics
│   ├── btc_vol_regimes.csv       # Regime classifications
│   ├── btc_technical_indicators.csv # 18 technical indicators
│   ├── btc_vrp_analysis.csv      # Variance risk premium data
│   ├── btc_higher_order_vol.csv  # VoV, skew, kurtosis
│   ├── btc_event_vol_profiles.csv # Halving/event vol patterns
│   ├── btc_*_backtest*.csv       # Portfolio backtest results (3 tiers)
│   ├── btc_walkforward_*.csv     # Walk-forward optimization results
│   └── btc_monte_carlo_results.csv
├── reports/                      # Research & performance reports
│   ├── data_foundation_report.txt
│   ├── btc_vol_research_report.txt
│   ├── btc_system_final_report.txt
│   └── btc_stress_test_report.txt
├── live/                         # Live trading data
│   ├── testnet_portfolio.json    # Current testnet state
│   ├── confidence_trades.json    # 42 orders with confidence scores
│   ├── btc_monitor_report.json   # Latest scan output
│   ├── btc_paper_trades.csv      # Paper trade log
│   └── telegram_*.txt            # Telegram message samples
├── profitable_trades/            # Trade analysis
│   ├── TRADE_ANALYSIS.md         # Comprehensive P&L analysis
│   └── positions_summary.csv     # Position-level breakdown
└── README.md
```

## Data Sources

- **Deribit Public API**: BTC index price, perpetual OHLCV, DVOL (implied vol index)
- **CoinGecko API**: Historical BTC price data (fallback)
- **Computed**: RV (close-close, Parkinson, Yang-Zhang), VRP, VoV, regime classification, all technical indicators

## Tech Stack

- Python 3.12
- NumPy, httpx
- Deribit API (testnet for execution)
- Telegram Bot API (for signal alerts)

## License

Private research project. All rights reserved.
