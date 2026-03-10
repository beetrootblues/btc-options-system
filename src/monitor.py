"""BTC Options System Monitor
==============================================================================
Orchestrates the signal engine + execution engine, generates reports,
and formats Telegram messages.

v3.0: Live testnet execution with instrument resolution.
"""

import sys
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

import httpx

sys.path.insert(0, '/home/user/files')

from btc_options_system.signal_engine import DeribitSignalEngine, MarketSnapshot, LiveSignal

# Try to import execution engine
try:
    from btc_options_system.execution_engine import DeribitExecutionEngine
    HAS_EXEC_ENGINE = True
except ImportError:
    HAS_EXEC_ENGINE = False

TESTNET_BASE = 'https://test.deribit.com/api/v2'


class InstrumentResolver:
    """Resolves signal engine's human-readable instruments to actual Deribit
    instrument names (e.g. 'BTC-28MAR26-70000-C').

    Queries /public/get_instruments once per scan and caches results.
    Finds the closest available strike/expiry to what the signal requests.
    """

    def __init__(self, testnet: bool = True):
        self.base_url = TESTNET_BASE if testnet else 'https://www.deribit.com/api/v2'
        self.client = httpx.Client(timeout=30)
        self._instruments_cache: Dict[str, list] = {}
        self._cache_ts: float = 0
        self._cache_ttl: float = 300  # 5 min cache

    def _fetch_instruments(self, currency: str = 'BTC', kind: str = 'option') -> list:
        """Fetch all active instruments from Deribit."""
        now = time.time()
        cache_key = f'{currency}_{kind}'
        if cache_key in self._instruments_cache and (now - self._cache_ts) < self._cache_ttl:
            return self._instruments_cache[cache_key]

        try:
            r = self.client.get(
                f'{self.base_url}/public/get_instruments',
                params={'currency': currency, 'kind': kind, 'expired': 'false'}
            )
            data = r.json()
            instruments = data.get('result', [])
            self._instruments_cache[cache_key] = instruments
            self._cache_ts = now
            print(f'  RESOLVER: Fetched {len(instruments)} active {currency} {kind} instruments')
            return instruments
        except Exception as e:
            print(f'  RESOLVER ERROR: {e}')
            return []

    def _find_closest_expiry(self, target_date_str: str, instruments: list) -> int:
        """Find the closest expiry timestamp to the target date.

        Args:
            target_date_str: 'YYYY-MM-DD' from signal engine
            instruments: list of Deribit instrument dicts

        Returns:
            expiration_timestamp (ms) of the closest available expiry
        """
        try:
            target = datetime.strptime(target_date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            target = datetime.now(timezone.utc) + timedelta(days=14)

        target_ts = target.timestamp() * 1000

        # Get unique expiry timestamps (future only)
        expiries = set()
        for inst in instruments:
            exp_ts = inst.get('expiration_timestamp', 0)
            if exp_ts > time.time() * 1000:
                expiries.add(exp_ts)

        if not expiries:
            return 0

        closest = min(expiries, key=lambda x: abs(x - target_ts))
        closest_dt = datetime.fromtimestamp(closest / 1000, tz=timezone.utc)
        print(f'  RESOLVER: Target expiry {target_date_str} -> closest available: '
              f'{closest_dt.strftime("%Y-%m-%d")} ({closest_dt.strftime("%d%b%y").upper()})')
        return closest

    def _find_closest_strike(self, target_strike: float, expiry_ts: int,
                              option_type: str, instruments: list) -> Optional[dict]:
        """Find the instrument with the closest strike at given expiry."""
        otype = option_type.lower()
        if otype in ('straddle', 'strangle'):
            otype = 'call'  # Will resolve both call and put separately

        candidates = [
            inst for inst in instruments
            if inst.get('expiration_timestamp') == expiry_ts
            and inst.get('option_type', '').lower() == otype
        ]

        if not candidates:
            return None

        best = min(candidates, key=lambda x: abs(x.get('strike', 0) - target_strike))
        actual_strike = best.get('strike', 0)
        if abs(actual_strike - target_strike) > target_strike * 0.1:
            print(f'  RESOLVER WARNING: Strike mismatch > 10%: '
                  f'target={target_strike:.0f}, nearest={actual_strike:.0f}')
        return best

    def resolve_signal(self, signal: dict, btc_price: float) -> List[dict]:
        """Resolve a LiveSignal into one or more executable order dicts.

        Returns list of dicts with fields the execution engine expects:
            deribit_instrument, direction, size_btc, mid_price_btc,
            mid_price_usd, deribit_strike, deribit_expiry, strategy_name,
            option_type, stop_loss, take_profit

        Multi-leg strategies return multiple order dicts.
        """
        instruments = self._fetch_instruments('BTC', 'option')
        if not instruments:
            print('  RESOLVER: No instruments available -- cannot resolve')
            return []

        signal_type = signal.get('signal_type', '')
        strategy = signal.get('strategy_name', '')
        direction = signal.get('direction', 'long')
        target_strike = signal.get('suggested_strike', 0)
        target_expiry = signal.get('suggested_expiry', '')
        total_size = signal.get('suggested_size_btc', 0.1)
        max_loss = signal.get('max_loss_usd', 0)
        legs = signal.get('legs', [])

        # Find closest expiry
        expiry_ts = self._find_closest_expiry(target_expiry, instruments)
        if not expiry_ts:
            print('  RESOLVER: No valid expiry found')
            return []

        expiry_dt = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
        expiry_str = expiry_dt.strftime('%Y-%m-%d')

        orders = []

        # -----------------------------------------------------------
        # IRON CONDOR (Strategy A): 4 legs
        # -----------------------------------------------------------
        if signal_type == 'iron_condor' and legs:
            per_leg_size = round(total_size / 4, 6)
            for leg in legs:
                leg_strike = leg.get('strike', target_strike)
                leg_type = leg.get('type', 'call')
                leg_dir = leg.get('direction', 'sell')

                inst = self._find_closest_strike(leg_strike, expiry_ts, leg_type, instruments)
                if not inst:
                    print(f'  RESOLVER: Could not find {leg_type} @ {leg_strike}')
                    continue

                mid = self._get_mid_price(inst['instrument_name'])
                orders.append(self._build_order(
                    instrument=inst,
                    direction='long' if leg_dir == 'buy' else 'short',
                    size_btc=per_leg_size,
                    mid_price_btc=mid,
                    btc_price=btc_price,
                    strategy=strategy,
                    expiry_str=expiry_str,
                    max_loss=max_loss / len(legs),
                ))

        # -----------------------------------------------------------
        # STRADDLE (Strategies C, D): 2 legs -- call + put at same strike
        # -----------------------------------------------------------
        elif signal_type in ('short_straddle', 'long_straddle') or \
             (legs and len(legs) == 2 and
              any(l.get('type') == 'call' for l in legs) and
              any(l.get('type') == 'put' for l in legs)):

            per_leg_size = round(total_size / 2, 6)

            for opt_type in ('call', 'put'):
                inst = self._find_closest_strike(target_strike, expiry_ts, opt_type, instruments)
                if not inst:
                    print(f'  RESOLVER: Could not find {opt_type} @ {target_strike}')
                    continue

                mid = self._get_mid_price(inst['instrument_name'])
                orders.append(self._build_order(
                    instrument=inst,
                    direction=direction,
                    size_btc=per_leg_size,
                    mid_price_btc=mid,
                    btc_price=btc_price,
                    strategy=strategy,
                    expiry_str=expiry_str,
                    max_loss=max_loss / 2,
                ))

        # -----------------------------------------------------------
        # SINGLE LEG (Strategy B): one call or put
        # -----------------------------------------------------------
        else:
            opt_type = signal.get('option_type', 'call')
            if opt_type in ('straddle', 'strangle'):
                opt_type = 'call'

            inst = self._find_closest_strike(target_strike, expiry_ts, opt_type, instruments)
            if not inst:
                print(f'  RESOLVER: Could not find {opt_type} @ {target_strike}')
                return []

            mid = self._get_mid_price(inst['instrument_name'])
            orders.append(self._build_order(
                instrument=inst,
                direction=direction,
                size_btc=total_size,
                mid_price_btc=mid,
                btc_price=btc_price,
                strategy=strategy,
                expiry_str=expiry_str,
                max_loss=max_loss,
            ))

        return orders

    def _get_mid_price(self, instrument_name: str) -> float:
        """Fetch current mid price (best_bid + best_ask / 2) for an instrument."""
        try:
            r = self.client.get(
                f'{self.base_url}/public/get_order_book',
                params={'instrument_name': instrument_name, 'depth': 1}
            )
            data = r.json()
            result = data.get('result', {})
            best_bid = result.get('best_bid_price', 0) or 0
            best_ask = result.get('best_ask_price', 0) or 0

            if best_bid and best_ask:
                mid = (best_bid + best_ask) / 2
            elif best_ask:
                mid = best_ask
            elif best_bid:
                mid = best_bid
            else:
                mid = result.get('mark_price', 0.001)

            print(f'  PRICE: {instrument_name} bid={best_bid:.6f} ask={best_ask:.6f} mid={mid:.6f} BTC')
            return mid
        except Exception as e:
            print(f'  PRICE ERROR ({instrument_name}): {e}')
            return 0.001

    def _build_order(self, instrument: dict, direction: str, size_btc: float,
                     mid_price_btc: float, btc_price: float, strategy: str,
                     expiry_str: str, max_loss: float) -> dict:
        """Build order dict matching execution engine's expected format."""
        inst_name = instrument.get('instrument_name', 'UNKNOWN')
        strike = instrument.get('strike', 0)
        opt_type = instrument.get('option_type', 'call')

        # Deribit minimum order size is 0.1 BTC for options
        size_btc = max(round(size_btc, 1), 0.1)

        return {
            'deribit_instrument': inst_name,
            'direction': direction,
            'size_btc': size_btc,
            'mid_price_btc': mid_price_btc,
            'mid_price_usd': mid_price_btc * btc_price,
            'deribit_strike': strike,
            'deribit_expiry': expiry_str,
            'strategy_name': strategy,
            'option_type': opt_type,
            'stop_loss': max_loss * 1.5 if direction == 'long' else 0,
            'take_profit': 0,
        }


class SystemMonitor:
    """Main system monitor: scans market, resolves instruments,
    executes on testnet, and formats messages."""

    def __init__(self, equity: float = 100.0, lookback_days: int = 120,
                 execute: bool = True):
        self.equity = equity
        self.execute = execute and HAS_EXEC_ENGINE
        self.signal_engine = DeribitSignalEngine(
            lookback_days=lookback_days,
            equity=equity,
        )
        self.resolver = InstrumentResolver(testnet=True)

        # Execution engine in TESTNET mode (not paper)
        self.exec_engine = None
        if HAS_EXEC_ENGINE:
            try:
                self.exec_engine = DeribitExecutionEngine(
                    testnet=True, paper_mode=not self.execute
                )
            except Exception as e:
                print(f'Execution engine init error: {e}')

    def generate_report(self) -> dict:
        """Run full scan, resolve instruments, execute trades, generate report."""
        # 1. Run signal scan
        snapshot, signals = self.signal_engine.scan()

        # 2. Resolve instruments + execute (if enabled)
        execution_results = []
        if signals and self.execute and self.exec_engine:
            execution_results = self._execute_signals(signals, snapshot.btc_price)

        # 3. Build report
        report = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'btc_price': snapshot.btc_price,
            'market_snapshot': snapshot.to_dict(),
            'signal_count': len(signals),
            'signals': [s.to_dict() for s in signals],
            'execution_results': execution_results,
            'portfolio': self._get_portfolio_summary(snapshot.btc_price),
            'equity': self.equity,
        }

        # Save report
        report_path = '/home/user/files/btc_options_system/btc_monitor_report.json'
        try:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, 'w') as f:
                json.dump(report, f, indent=2, default=str)
        except Exception as e:
            print(f'Warning: Could not save report: {e}')

        return report

    def _execute_signals(self, signals: List[LiveSignal], btc_price: float) -> list:
        """Resolve instruments and place testnet orders for each signal."""
        results = []

        for sig in signals:
            sig_dict = sig.to_dict()
            print(f'\n{"=" * 60}')
            print(f'EXECUTING: {sig.strategy_name}')
            print(f'{"=" * 60}')

            # Resolve to actual Deribit instruments
            orders = self.resolver.resolve_signal(sig_dict, btc_price)

            if not orders:
                results.append({
                    'strategy': sig.strategy_name,
                    'status': 'FAILED',
                    'reason': 'No matching instruments found',
                    'orders': [],
                })
                continue

            # Place each order
            order_results = []
            for order in orders:
                print(f'  PLACING: {order["direction"]} {order["deribit_instrument"]} '
                      f'size={order["size_btc"]:.1f} BTC @ {order["mid_price_btc"]:.6f} BTC')

                trade = self.exec_engine.place_order(order)
                if trade:
                    order_results.append({
                        'instrument': trade.instrument,
                        'direction': trade.direction,
                        'size_btc': trade.size_btc,
                        'entry_price_btc': trade.entry_price_btc,
                        'entry_price_usd': trade.entry_price_usd,
                        'order_id': trade.order_id,
                        'execution_mode': trade.execution_mode,
                        'trade_id': trade.trade_id,
                    })
                else:
                    order_results.append({
                        'instrument': order['deribit_instrument'],
                        'direction': order['direction'],
                        'size_btc': order['size_btc'],
                        'status': 'FAILED',
                    })

            status = 'EXECUTED' if all(
                r.get('order_id') for r in order_results
            ) else 'PARTIAL' if any(
                r.get('order_id') for r in order_results
            ) else 'FAILED'

            results.append({
                'strategy': sig.strategy_name,
                'status': status,
                'orders': order_results,
            })

        return results

    def _get_portfolio_summary(self, btc_price: float) -> dict:
        """Get portfolio summary from execution engine."""
        if self.exec_engine:
            try:
                # Fetch live testnet account if authenticated
                testnet_summary = {}
                if self.execute and self.exec_engine.access_token:
                    acct = self.exec_engine.get_account_summary()
                    if acct:
                        testnet_summary = {
                            'equity_btc': acct.get('equity', 0),
                            'balance_btc': acct.get('balance', 0),
                            'initial_margin': acct.get('initial_margin', 0),
                            'available_funds': acct.get('available_funds', 0),
                        }

                summary = self.exec_engine.get_portfolio_summary(btc_price)
                return {
                    'summary': summary,
                    'testnet_account': testnet_summary,
                    'open_positions': [
                        {
                            'instrument': p.instrument,
                            'direction': p.direction,
                            'size_btc': p.size_btc,
                            'entry_price_usd': p.entry_price_usd,
                            'strategy': p.strategy,
                            'order_id': p.order_id,
                            'execution_mode': p.execution_mode,
                        }
                        for p in self.exec_engine.open_positions
                    ],
                }
            except Exception as e:
                print(f'Portfolio summary error: {e}')

        return {
            'summary': {
                'open_positions': 0, 'closed_positions': 0,
                'total_invested_usd': 0, 'unrealized_pnl_usd': 0,
                'realized_pnl_usd': 0, 'total_pnl_usd': 0,
            },
            'testnet_account': {},
            'open_positions': [],
        }

    def format_telegram_message(self, report: dict) -> str:
        """Format report as HTML Telegram message with execution results."""
        snap = report.get('market_snapshot', {})
        signals = report.get('signals', [])
        exec_results = report.get('execution_results', [])
        portfolio = report.get('portfolio', {})
        summary = portfolio.get('summary', {})
        testnet_acct = portfolio.get('testnet_account', {})

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

        regime_icon = {
            'LOW': '\U0001f7e2', 'MEDIUM': '\U0001f7e1',
            'HIGH': '\U0001f7e0', 'CRISIS': '\U0001f534',
        }.get(regime, '\u26aa')

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

        # Signals + Execution
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

                # Confidence score
                conf_score = sig.get('confidence_score', 0)
                if conf_score:
                    lines.append(f'  Confidence: {conf_score:.4f}')

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

                # Execution results for this signal
                if exec_results and i <= len(exec_results):
                    er = exec_results[i - 1]
                    status = er.get('status', 'N/A')
                    status_icon = {
                        'EXECUTED': '\u2705', 'PARTIAL': '\u26a0\ufe0f', 'FAILED': '\u274c'
                    }.get(status, '\u2753')

                    lines.append('')
                    lines.append(f'  {status_icon} <b>Execution: {status}</b>')
                    for o in er.get('orders', []):
                        mode = o.get('execution_mode', 'paper')
                        oid = o.get('order_id', '')
                        inst = o.get('instrument', 'UNKNOWN')
                        d = o.get('direction', '')
                        sz = o.get('size_btc', 0)
                        px = o.get('entry_price_btc', 0)

                        if oid:
                            lines.append(
                                f'    {d.upper()} {inst} {sz:.1f} BTC '
                                f'@ {px:.6f} BTC [{mode}]'
                            )
                            lines.append(f'    Order: <code>{oid}</code>')
                        else:
                            lines.append(
                                f'    {d.upper()} {inst} {sz:.1f} BTC '
                                f'[{o.get("status", "FAILED")}]'
                            )

                lines.append('')
        else:
            lines.append('\U0001f4ca <b>No Active Signals</b>')
            lines.append('  All 4 strategies checked - no entry conditions met.')

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

        # Testnet Account
        if testnet_acct:
            eq = testnet_acct.get('equity_btc', 0)
            avail = testnet_acct.get('available_funds', 0)
            margin = testnet_acct.get('initial_margin', 0)
            lines.append('\U0001f3e6 <b>Testnet Account</b>')
            lines.append(f'  Equity: {eq:.4f} BTC (${eq * btc_price:,.0f})')
            lines.append(f'  Available: {avail:.4f} BTC')
            lines.append(f'  Margin Used: {margin:.4f} BTC')
            lines.append('')

        # Paper Portfolio
        open_pos = summary.get('open_positions', 0)
        total_pnl = summary.get('total_pnl_usd', 0)
        if open_pos > 0:
            lines.append(f'\U0001f4bc <b>Paper Portfolio</b>')
            lines.append(f'  Open: {open_pos} | PnL: ${total_pnl:+,.2f}')
            for pos in portfolio.get('open_positions', [])[:5]:
                mode_tag = f' [{pos.get("execution_mode", "paper")}]' if pos.get('order_id') else ''
                lines.append(
                    f'  - {pos["instrument"]} ({pos["direction"]}) '
                    f'{pos["size_btc"]:.1f} BTC @ ${pos["entry_price_usd"]:,.2f}{mode_tag}'
                )
            if open_pos > 5:
                lines.append(f'  ... and {open_pos - 5} more')
            lines.append('')

        lines.append(f'<i>Signal Equity: {report.get("equity", 0):.2f} BTC</i>')

        return '\n'.join(lines)
