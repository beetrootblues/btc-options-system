"""BTC Options System Monitor
==============================================================================
Orchestrates the signal engine + execution engine, generates reports,
and formats Telegram messages.
"""

import sys
import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

sys.path.insert(0, '/home/user/files')

from btc_options_system.signal_engine import DeribitSignalEngine, MarketSnapshot, LiveSignal

# Try to import execution engine for portfolio tracking
try:
    from btc_options_system.execution_engine import DeribitExecutionEngine
    HAS_EXEC_ENGINE = True
except ImportError:
    HAS_EXEC_ENGINE = False


class SystemMonitor:
    """Main system monitor: scans market, generates reports, formats messages."""

    def __init__(self, equity: float = 100.0, lookback_days: int = 120, execute: bool = False):
        self.equity = equity
        self.signal_engine = DeribitSignalEngine(
            lookback_days=lookback_days,
            equity=equity,
        )
        # Execution engine for portfolio tracking (paper mode)
        self.exec_engine = None
        if HAS_EXEC_ENGINE:
            try:
                self.exec_engine = DeribitExecutionEngine(
                    testnet=True, paper_mode=True
                )
            except Exception:
                pass

    def generate_report(self) -> dict:
        """Run full scan and generate comprehensive report dict."""
        # Run signal scan
        snapshot, signals = self.signal_engine.scan()

        # Build report
        report = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'btc_price': snapshot.btc_price,
            'market_snapshot': snapshot.to_dict(),
            'signal_count': len(signals),
            'total_signal_count': len(signals),
            'signals': [s.to_dict() for s in signals],
            'portfolio': self._get_portfolio_summary(snapshot.btc_price),
            'equity': self.equity,
            'profit_evaluation': {'exit_count': 0},
            'executions': [],
        }

        # Save report to file
        report_path = '/home/user/files/btc_options_system/btc_monitor_report.json'
        try:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, 'w') as f:
                json.dump(report, f, indent=2, default=str)
        except Exception as e:
            print(f'Warning: Could not save report: {e}')

        return report

    def _get_portfolio_summary(self, btc_price: float) -> dict:
        """Get portfolio summary from execution engine."""
        if self.exec_engine:
            try:
                summary = self.exec_engine.get_portfolio_summary(btc_price)
                return {
                    'summary': summary,
                    'open_positions': [
                        {
                            'instrument': p.instrument,
                            'direction': p.direction,
                            'size_btc': p.size_btc,
                            'entry_price_usd': p.entry_price_usd,
                            'strategy': p.strategy,
                        }
                        for p in self.exec_engine.open_positions
                    ],
                }
            except Exception:
                pass

        return {
            'summary': {
                'open_positions': 0,
                'closed_positions': 0,
                'total_invested_usd': 0,
                'unrealized_pnl_usd': 0,
                'realized_pnl_usd': 0,
                'total_pnl_usd': 0,
            },
            'open_positions': [],
        }

    def format_telegram_message(self, report: dict) -> str:
        """Format report as HTML Telegram message."""
        snap = report.get('market_snapshot', {})
        signals = report.get('signals', [])
        portfolio = report.get('portfolio', {})
        summary = portfolio.get('summary', {})

        btc_price = report.get('btc_price', 0)
        regime = snap.get('regime', 'N/A')
        rv_30d = snap.get('rv_cc_30d', 0)
        iv_30d = snap.get('iv_30d', 0)
        vrp = snap.get('vrp_30d', 0)
        vrp_z = snap.get('vrp_zscore', 0)
        rsi = snap.get('rsi_14', 0)
        vov = snap.get('vov_30d', 0)
        ret_5d = snap.get('ret_5d', 0)
        rv_pctl = snap.get('rv_percentile', 0)
        atr = snap.get('atr_14', 0)

        # Regime emoji mapping
        regime_icon = {
            'LOW': '\U0001f7e2',
            'MEDIUM': '\U0001f7e1',
            'HIGH': '\U0001f7e0',
            'CRISIS': '\U0001f534',
        }.get(regime, '\u26aa')

        # RSI condition
        if rsi > 70:
            rsi_label = 'Overbought'
        elif rsi < 30:
            rsi_label = 'Oversold'
        else:
            rsi_label = 'Neutral'

        lines = []
        lines.append('<b>BTC Options Signal Monitor</b>')
        lines.append(f'<i>{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</i>')
        lines.append('')

        # Market Snapshot
        lines.append(f'{regime_icon} <b>Market Snapshot</b>')
        lines.append(f'  BTC: <b>${btc_price:,.2f}</b>')
        lines.append(f'  Regime: <b>{regime}</b>')
        lines.append(f'  RV(30d): {rv_30d*100:.1f}% | IV(30d): {iv_30d*100:.1f}%')
        lines.append(f'  VRP: {vrp*100:.1f}% (z={vrp_z:.2f})')
        lines.append(f'  RSI(14): {rsi:.1f} ({rsi_label})')
        lines.append(f'  VoV(30d): {vov:.4f}')
        lines.append(f'  ATR(14): ${atr:,.0f}')
        lines.append(f'  5d Return: {ret_5d*100:+.1f}%')
        lines.append(f'  RV Percentile: {rv_pctl:.0f}%')
        lines.append('')

        # Signals
        if signals:
            lines.append(f'\U0001f514 <b>{len(signals)} Signal(s) Detected</b>')
            lines.append('')
            for i, sig in enumerate(signals, 1):
                conf_icon = {'HIGH': '\U0001f7e2', 'MEDIUM': '\U0001f7e1', 'LOW': '\U0001f535'}.get(
                    sig.get('confidence', ''), '\u26aa')
                lines.append(f'{conf_icon} <b>Signal {i}: {sig["strategy_name"]}</b>')
                lines.append(f'  Type: {sig["signal_type"]}')
                lines.append(f'  Direction: {sig["direction"].upper()}')
                lines.append(f'  {sig["description"]}')
                size_btc = sig['suggested_size_btc']
                if size_btc >= 0.01:
                    lines.append(f'  Size: {size_btc:.4f} BTC (${size_btc * btc_price:,.2f})')
                else:
                    lines.append(f'  Size: {size_btc:.6f} BTC (${size_btc * btc_price:,.2f})')
                lines.append(f'  Max Loss: ${sig["max_loss_usd"]:,.2f}')
                lines.append(f'  Expiry: {sig["suggested_expiry"]}')

                # Legs
                legs = sig.get('legs', [])
                if legs:
                    leg_strs = []
                    for leg in legs:
                        leg_strs.append(
                            f'{leg["direction"].upper()} {leg["type"].upper()} '
                            f'@ {leg["strike"]:,.0f}'
                        )
                    lines.append(f'  Legs: {" | ".join(leg_strs)}')

                # Conditions
                conds = sig.get('conditions_met', [])
                if conds:
                    lines.append(f'  Conditions:')
                    for c in conds:
                        lines.append(f'    - {c}')
                lines.append('')
        else:
            lines.append('\U0001f4ca <b>No Active Signals</b>')
            lines.append('  All 4 strategies checked - no entry conditions met.')

            # Show why each strategy didn\'t trigger
            reasons = []
            if regime != 'CRISIS':
                reasons.append(f'A (Iron Condor): Need CRISIS regime, got {regime}')
            else:
                if vrp_z <= -0.5:
                    reasons.append(f'A (Iron Condor): VRP z={vrp_z:.2f} <= -0.5')
                elif vov >= snap.get('vov_5d_avg', 0):
                    reasons.append('A (Iron Condor): VoV not declining')

            if regime not in ('LOW', 'MEDIUM'):
                reasons.append(f'B (Momentum): Need LOW/MEDIUM, got {regime}')
            else:
                reasons.append(f'B (Momentum): 5d ret={ret_5d*100:+.1f}%, need >5% or <-5%')

            reasons.append(f'C (Event Vol): IV/RV or regime conditions not met')

            if rv_pctl <= 90:
                reasons.append(f'D (Mean Rev): RV pctl={rv_pctl:.0f}% (need >90% or <15%)')

            if reasons:
                lines.append('')
                lines.append('<i>Strategy status:</i>')
                for r in reasons:
                    lines.append(f'  {r}')
            lines.append('')

        # Portfolio
        open_pos = summary.get('open_positions', 0)
        total_pnl = summary.get('total_pnl_usd', 0)
        if open_pos > 0:
            lines.append(f'\U0001f4bc <b>Portfolio</b>')
            lines.append(f'  Open: {open_pos} | PnL: ${total_pnl:+,.2f}')
            for pos in portfolio.get('open_positions', []):
                lines.append(
                    f'  - {pos["instrument"]} ({pos["direction"]}) '
                    f'{pos["size_btc"]:.4f} BTC @ ${pos["entry_price_usd"]:,.2f}'
                )
            lines.append('')

        lines.append(f'<i>Equity: ${report.get("equity", 0):,.2f}</i>')

        return '\n'.join(lines)
