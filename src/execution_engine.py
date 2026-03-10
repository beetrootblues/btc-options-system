"""BTC Options Execution Engine
==========================================================
Dual-mode execution: every order placed on Deribit testnet is ALSO
logged as a paper trade for local tracking and P&L analysis.

Deribit Testnet: https://test.deribit.com/api/v2
Deribit Mainnet: https://www.deribit.com/api/v2
"""

import sys
import os
import json
import time
import csv
import math
import hmac
import hashlib
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple

import httpx

sys.path.insert(0, '/home/user/files')

TESTNET_BASE = 'https://test.deribit.com/api/v2'
LIVE_BASE = 'https://www.deribit.com/api/v2'

# Default credentials for testnet (safe to embed -- testnet only)
DEFAULT_TESTNET_KEY = 'lPPL4zNY'
DEFAULT_TESTNET_SECRET = 'VC0huhVd6fTEzZjE2MLD5xUWlK7lgJFQjGGxKfTIjQA'


@dataclass
class PaperTrade:
    """Local record of every trade placed (paper or testnet)."""
    trade_id: str
    timestamp: str
    strategy: str
    instrument: str
    direction: str
    size_btc: float
    entry_price_btc: float
    entry_price_usd: float
    strike: float
    expiry: str
    option_type: str
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    status: str = 'open'
    exit_price_usd: float = 0.0
    exit_timestamp: str = ''
    pnl_usd: float = 0.0
    exit_reason: str = ''
    order_id: str = ''           # Deribit order ID (empty for pure paper)
    execution_mode: str = 'paper'  # 'paper' or 'testnet' or 'live'


class DeribitExecutionEngine:
    """Execution engine with dual-mode: testnet orders + local paper tracking.

    Default behaviour (testnet=True, paper_mode=False):
      - Authenticates to Deribit testnet with default credentials
      - Places real orders on testnet
      - Also logs every order as a PaperTrade in CSV for local tracking

    Pure paper mode (paper_mode=True):
      - No API calls, just local CSV tracking
    """

    def __init__(self, api_key=None, api_secret=None, testnet=True, paper_mode=False):
        self.testnet = testnet
        self.base_url = TESTNET_BASE if testnet else LIVE_BASE
        self.paper_mode = paper_mode

        # Use default testnet credentials if none provided
        if testnet and not api_key:
            api_key = DEFAULT_TESTNET_KEY
            api_secret = DEFAULT_TESTNET_SECRET

        self.api_key = api_key
        self.api_secret = api_secret
        self.client = httpx.Client(timeout=30)
        self.access_token = None
        self.token_expiry = 0

        # Position tracking
        self.open_positions: List[PaperTrade] = []
        self.closed_positions: List[PaperTrade] = []
        self.trade_counter = 0
        self.paper_trades_file = '/home/user/files/btc_options_system/btc_paper_trades.csv'
        self._load_paper_trades()

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------
    def authenticate(self) -> bool:
        """Authenticate with Deribit API using client credentials."""
        if not self.api_key or not self.api_secret:
            print('No API credentials -- running in paper mode only')
            self.paper_mode = True
            return False
        try:
            r = self.client.get(
                self.base_url + '/public/auth',
                params={
                    'grant_type': 'client_credentials',
                    'client_id': self.api_key,
                    'client_secret': self.api_secret,
                },
            )
            data = r.json()
            result = data.get('result')
            if result:
                self.access_token = result['access_token']
                self.token_expiry = time.time() + result.get('expires_in', 900) - 60
                print(f'Authenticated successfully (testnet={self.testnet})')
                return True
            else:
                err = data.get('error', {}).get('message', 'unknown')
                print(f'Auth failed: {err}')
                return False
        except Exception as e:
            print(f'Auth error: {e}')
            return False

    def _ensure_auth(self):
        """Re-authenticate if token expired."""
        if self.paper_mode:
            return
        if not self.access_token or time.time() >= self.token_expiry:
            self.authenticate()

    def _auth_headers(self) -> dict:
        """Return Authorization header if we have a valid token."""
        if self.access_token and time.time() < self.token_expiry:
            return {'Authorization': f'Bearer {self.access_token}'}
        return {}

    # -----------------------------------------------------------------
    # API helpers
    # -----------------------------------------------------------------
    def _public_get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make public (unauthenticated) GET request."""
        try:
            r = self.client.get(self.base_url + endpoint, params=params or {})
            data = r.json()
            return data.get('result')
        except Exception as e:
            print(f'Public API error ({endpoint}): {e}')
            return None

    def _private_get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make authenticated GET request to a private endpoint."""
        self._ensure_auth()
        try:
            r = self.client.get(
                self.base_url + endpoint,
                params=params or {},
                headers=self._auth_headers(),
            )
            data = r.json()
            if 'error' in data:
                err = data['error']
                print(f'API error ({endpoint}): {err.get("message", err)}')
                return None
            return data.get('result')
        except Exception as e:
            print(f'Private API error ({endpoint}): {e}')
            return None

    # -----------------------------------------------------------------
    # Paper trade persistence
    # -----------------------------------------------------------------
    def _load_paper_trades(self):
        """Load existing paper trades from CSV."""
        if not os.path.exists(self.paper_trades_file):
            return
        try:
            with open(self.paper_trades_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trade = PaperTrade(
                        trade_id=row['trade_id'],
                        timestamp=row['timestamp'],
                        strategy=row['strategy'],
                        instrument=row['instrument'],
                        direction=row['direction'],
                        size_btc=float(row['size_btc']),
                        entry_price_btc=float(row['entry_price_btc']),
                        entry_price_usd=float(row['entry_price_usd']),
                        strike=float(row['strike']),
                        expiry=row['expiry'],
                        option_type=row['option_type'],
                        stop_loss_price=float(row.get('stop_loss_price', 0)),
                        take_profit_price=float(row.get('take_profit_price', 0)),
                        status=row.get('status', 'open'),
                        exit_price_usd=float(row.get('exit_price_usd', 0)),
                        exit_timestamp=row.get('exit_timestamp', ''),
                        pnl_usd=float(row.get('pnl_usd', 0)),
                        exit_reason=row.get('exit_reason', ''),
                        order_id=row.get('order_id', ''),
                        execution_mode=row.get('execution_mode', 'paper'),
                    )
                    if trade.status == 'open':
                        self.open_positions.append(trade)
                    else:
                        self.closed_positions.append(trade)
        except Exception as e:
            print(f'Error loading paper trades: {e}')
            self.open_positions = []
            self.closed_positions = []

    def _save_paper_trades(self):
        """Persist all paper trades (open + closed) to CSV."""
        all_trades = self.open_positions + self.closed_positions
        if not all_trades:
            return
        os.makedirs(os.path.dirname(self.paper_trades_file), exist_ok=True)
        with open(self.paper_trades_file, 'w', newline='') as f:
            fields = list(asdict(all_trades[0]).keys())
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for t in all_trades:
                writer.writerow(asdict(t))

    def _next_trade_id(self) -> str:
        self.trade_counter += 1
        return f'PAPER-{datetime.utcnow().strftime("%Y%m%d")}-{self.trade_counter:04d}'

    # -----------------------------------------------------------------
    # Tick size alignment (Deribit stepped tick sizes)
    # -----------------------------------------------------------------
    def _align_price_to_tick(self, price: float, instrument_name: str) -> float:
        """Align a price to Deribit's tick size grid.

        Deribit uses stepped tick sizes for options. For example:
          - price < 0.005 BTC  -> tick = 0.0001
          - price >= 0.005 BTC -> tick = 0.0005
          - price >= 0.05 BTC  -> tick = 0.001 (some instruments)

        We fetch the instrument's tick_size_steps from the API and snap
        the price down to the nearest valid tick.
        """
        try:
            inst_data = self._public_get('/public/get_instrument', {
                'instrument_name': instrument_name
            })
            if not inst_data:
                # Fallback: round to 4 decimals
                return round(price, 4)

            tick_steps = inst_data.get('tick_size_steps', [])
            base_tick = inst_data.get('tick_size', 0.0001)

            if not tick_steps:
                # No steps -- use base tick
                tick = base_tick
            else:
                # tick_size_steps is a list of {above_price, tick_size}
                # sorted ascending by above_price threshold
                tick = base_tick
                for step in tick_steps:
                    threshold = step.get('above_price', 0)
                    if price >= threshold:
                        tick = step.get('tick_size', tick)
                    else:
                        break

            # Snap price DOWN to nearest tick multiple
            aligned = math.floor(price / tick) * tick
            aligned = round(aligned, 8)  # avoid floating point drift

            if aligned != price:
                print(f'  TICK ALIGN: {price:.8f} -> {aligned:.8f} '
                      f'(tick={tick}, instrument={instrument_name})')
            return aligned

        except Exception as e:
            print(f'  Tick alignment error: {e} -- using raw price')
            return round(price, 4)

    # -----------------------------------------------------------------
    # Order placement (dual-mode)
    # -----------------------------------------------------------------
    def place_order(self, signal_dict: dict) -> Optional[PaperTrade]:
        """Place order from signal engine output.

        In testnet mode: places real order on Deribit testnet AND logs paper trade.
        In paper mode: logs paper trade only.

        Args:
            signal_dict: Dict with keys:
                - deribit_instrument: e.g. 'BTC-28MAR26-70000-C'
                - direction: 'long' or 'short'
                - size_btc: float
                - mid_price_btc: option price in BTC
                - mid_price_usd: option price in USD
                - deribit_strike: float
                - deribit_expiry: str (YYYY-MM-DD)
                - strategy_name: str
                - option_type: 'call' or 'put'
                - stop_loss: float (USD)
                - take_profit: float (USD)

        Returns:
            PaperTrade on success, None on failure.
        """
        instrument = signal_dict.get('deribit_instrument', 'UNKNOWN')
        direction = signal_dict.get('direction', 'long')
        size = signal_dict.get('size_btc', 0.1)
        mid_price_btc = signal_dict.get('mid_price_btc', 0)
        mid_price_usd = signal_dict.get('mid_price_usd', 0)
        strike = signal_dict.get('deribit_strike', 0)
        expiry = signal_dict.get('deribit_expiry', '')
        strategy = signal_dict.get('strategy_name', signal_dict.get('strategy', ''))
        opt_type = signal_dict.get('option_type', 'call')
        side = 'buy' if direction == 'long' else 'sell'

        order_id = ''
        exec_mode = 'paper'

        # --- Testnet / live execution ---
        if not self.paper_mode:
            self._ensure_auth()
            if self.access_token:
                # Align price to Deribit's stepped tick size grid
                aligned_price = self._align_price_to_tick(mid_price_btc, instrument)
                params = {
                    'instrument_name': instrument,
                    'amount': size,
                    'type': 'limit',
                    'price': aligned_price,
                    'post_only': 'true',
                    'time_in_force': 'good_til_cancelled',
                }
                result = self._private_get(f'/private/{side}', params=params)
                if result:
                    order = result.get('order', {})
                    order_id = str(order.get('order_id', ''))
                    exec_mode = 'testnet' if self.testnet else 'live'
                    print(f'  TESTNET ORDER: {order_id} {side} {instrument} '
                          f'size={size:.4f} BTC @ {mid_price_btc:.6f} BTC (${mid_price_usd:.2f})')
                else:
                    print(f'  ORDER FAILED: {side} {instrument} -- falling back to paper')
                    exec_mode = 'paper'
            else:
                print(f'  NOT AUTHENTICATED -- paper trade only')
                exec_mode = 'paper'

        # --- Always log a paper trade for local tracking ---
        trade = PaperTrade(
            trade_id=self._next_trade_id(),
            timestamp=datetime.utcnow().isoformat() + 'Z',
            strategy=strategy,
            instrument=instrument,
            direction=side,
            size_btc=size,
            entry_price_btc=mid_price_btc,
            entry_price_usd=mid_price_usd,
            strike=strike,
            expiry=expiry,
            option_type=opt_type,
            stop_loss_price=signal_dict.get('stop_loss', 0),
            take_profit_price=signal_dict.get('take_profit', 0),
            order_id=order_id,
            execution_mode=exec_mode,
        )
        self.open_positions.append(trade)
        self._save_paper_trades()
        print(f'  PAPER TRADE: {trade.trade_id} {trade.direction} {trade.instrument} '
              f'size={trade.size_btc:.4f} BTC @ ${trade.entry_price_usd:.2f} '
              f'[{exec_mode}]')
        return trade

    # -----------------------------------------------------------------
    # Testnet account queries
    # -----------------------------------------------------------------
    def get_account_summary(self, currency: str = 'BTC') -> Optional[dict]:
        """Fetch testnet account summary (equity, balance, margin)."""
        return self._private_get('/private/get_account_summary', {'currency': currency})

    def get_positions(self, currency: str = 'BTC', kind: str = 'option') -> Optional[list]:
        """Fetch open positions from testnet."""
        return self._private_get('/private/get_positions', {'currency': currency, 'kind': kind})

    def get_open_orders(self, currency: str = 'BTC') -> Optional[list]:
        """Fetch open orders from testnet."""
        return self._private_get('/private/get_open_orders_by_currency', {'currency': currency})

    def cancel_all(self) -> Optional[dict]:
        """Emergency: cancel ALL open orders on testnet."""
        return self._private_get('/private/cancel_all')

    def cancel_order(self, order_id: str) -> Optional[dict]:
        """Cancel a specific order by ID."""
        return self._private_get('/private/cancel', {'order_id': order_id})

    # -----------------------------------------------------------------
    # Position monitoring & exit management
    # -----------------------------------------------------------------
    def monitor_positions(self, spot_price: float) -> List[PaperTrade]:
        """Check all open paper positions against current market; apply exit rules.

        Returns list of positions closed this cycle.
        """
        closed_this_cycle = []
        remaining = []

        for pos in self.open_positions:
            # Parse expiry
            try:
                expiry_date = datetime.strptime(pos.expiry, '%Y-%m-%d')
            except (ValueError, TypeError):
                # Try Deribit-style DDMMMYY e.g. 28MAR26
                try:
                    expiry_date = datetime.strptime(pos.expiry, '%d%b%y')
                except (ValueError, TypeError):
                    expiry_date = datetime.utcnow() + timedelta(days=30)

            # Intrinsic value
            if pos.option_type == 'call':
                intrinsic = max(spot_price - pos.strike, 0)
            else:
                intrinsic = max(pos.strike - spot_price, 0)

            days_to_expiry = (expiry_date - datetime.utcnow()).days

            # Time value decay approximation
            time_value = pos.entry_price_usd * 0.5 * max(days_to_expiry / 30, 0)
            current_value = intrinsic + time_value

            if pos.direction == 'buy':
                pnl = (current_value - pos.entry_price_usd) * pos.size_btc
            else:
                pnl = (pos.entry_price_usd - current_value) * pos.size_btc

            # Check expiry
            if days_to_expiry <= 0:
                pos.status = 'closed'
                pos.exit_price_usd = intrinsic
                pos.exit_timestamp = datetime.utcnow().isoformat() + 'Z'
                pos.pnl_usd = pnl
                pos.exit_reason = 'expired'
                closed_this_cycle.append(pos)
                continue

            # Check stop loss
            if pos.stop_loss_price > 0 and current_value <= pos.stop_loss_price:
                pos.status = 'closed'
                pos.exit_price_usd = current_value
                pos.exit_timestamp = datetime.utcnow().isoformat() + 'Z'
                pos.pnl_usd = pnl
                pos.exit_reason = 'stop_loss'
                closed_this_cycle.append(pos)
                continue

            # Check take profit
            if pos.take_profit_price > 0 and current_value >= pos.take_profit_price:
                pos.status = 'closed'
                pos.exit_price_usd = current_value
                pos.exit_timestamp = datetime.utcnow().isoformat() + 'Z'
                pos.pnl_usd = pnl
                pos.exit_reason = 'take_profit'
                closed_this_cycle.append(pos)
                continue

            remaining.append(pos)

        self.closed_positions.extend(closed_this_cycle)
        self.open_positions = remaining
        self._save_paper_trades()
        return closed_this_cycle

    def get_portfolio_summary(self, spot_price: float) -> dict:
        """Return portfolio status dict with open/closed counts and P&L."""
        total_invested = sum(p.entry_price_usd * p.size_btc for p in self.open_positions)
        total_realized_pnl = sum(p.pnl_usd for p in self.closed_positions)

        unrealized_pnl = 0.0
        for pos in self.open_positions:
            if pos.option_type == 'call':
                intrinsic = max(spot_price - pos.strike, 0)
            else:
                intrinsic = max(pos.strike - spot_price, 0)

            try:
                expiry_date = datetime.strptime(pos.expiry, '%Y-%m-%d')
                days_to_expiry = (expiry_date - datetime.utcnow()).days
            except (ValueError, TypeError):
                try:
                    expiry_date = datetime.strptime(pos.expiry, '%d%b%y')
                    days_to_expiry = (expiry_date - datetime.utcnow()).days
                except (ValueError, TypeError):
                    days_to_expiry = 30

            time_value = pos.entry_price_usd * 0.5 * max(days_to_expiry / 30, 0)
            current_value = intrinsic + time_value

            if pos.direction == 'buy':
                unrealized_pnl += (current_value - pos.entry_price_usd) * pos.size_btc
            else:
                unrealized_pnl += (pos.entry_price_usd - current_value) * pos.size_btc

        return {
            'open_positions': len(self.open_positions),
            'closed_positions': len(self.closed_positions),
            'total_invested_usd': round(total_invested, 2),
            'unrealized_pnl_usd': round(unrealized_pnl, 2),
            'realized_pnl_usd': round(total_realized_pnl, 2),
            'total_pnl_usd': round(total_realized_pnl + unrealized_pnl, 2),
        }

    def manage_exits(self, spot_price: float) -> List[PaperTrade]:
        """Run full exit management cycle. Returns list of closed trades."""
        closed = self.monitor_positions(spot_price)
        for c in closed:
            print(f'  CLOSED: {c.trade_id} {c.instrument} '
                  f'reason={c.exit_reason} PnL=${c.pnl_usd:.2f}')
        return closed


def main():
    """Quick connectivity and auth test."""
    engine = DeribitExecutionEngine(testnet=True, paper_mode=False)
    print(f'Mode: testnet={engine.testnet}, paper_mode={engine.paper_mode}')
    print(f'Base URL: {engine.base_url}')

    # Authenticate
    auth_ok = engine.authenticate()
    print(f'Auth result: {auth_ok}')

    if auth_ok:
        # Account summary
        acct = engine.get_account_summary()
        if acct:
            print(f'\nAccount Summary:')
            print(f'  Equity: {acct.get("equity", "N/A")} BTC')
            print(f'  Balance: {acct.get("balance", "N/A")} BTC')
            print(f'  Margin: {acct.get("initial_margin", "N/A")} BTC')

        # Open positions
        positions = engine.get_positions()
        print(f'\nOpen positions: {len(positions) if positions else 0}')

        # Open orders
        orders = engine.get_open_orders()
        print(f'Open orders: {len(orders) if orders else 0}')

    # Get BTC price for portfolio summary
    try:
        r = httpx.get('https://www.deribit.com/api/v2/public/get_index_price',
                      params={'index_name': 'btc_usd'}, timeout=10)
        spot = r.json()['result']['index_price']
        print(f'\nBTC Price: ${spot:,.2f}')
        summary = engine.get_portfolio_summary(spot)
        print(f'Portfolio: {summary}')
    except Exception as e:
        print(f'Price fetch error: {e}')

    print('\nExecution engine ready')


if __name__ == '__main__':
    main()
