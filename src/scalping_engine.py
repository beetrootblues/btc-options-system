"""BTC Options Scalping Engine - Intraday Micro-Strategies
==============================================================================
5 high-frequency strategies for 3-minute scan cycles targeting ~20 signals/hour.
Uses 1-minute candles, real-time order book depth, and live Greeks from Deribit.

Strategies:
  E: Gamma Scalp        - Long ATM straddle when 1min RV spikes vs 5min RV
  F: IV Crush Scalp     - Short ATM when IV rank spikes >80th pctl intraday
  G: Order Book Imbalance - Directional OTM based on bid/ask depth ratio
  H: Micro-Momentum     - Continuation OTM on 3min breakout vs ATR
  I: Skew Arbitrage     - Risk reversal when put/call IV diverges >5%

v1.0 - 2026-03-10
"""

import sys
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field

import numpy as np
import httpx

sys.path.insert(0, '/home/user/files')

DERIBIT_PUBLIC = 'https://www.deribit.com/api/v2'


@dataclass
class ScalpSignal:
    """Signal from an intraday scalping strategy."""
    strategy_name: str
    signal_type: str
    direction: str
    description: str
    confidence: str
    confidence_score: float
    suggested_instrument: str
    suggested_strike: float
    suggested_expiry: str
    option_type: str
    suggested_size_btc: float
    max_loss_usd: float
    legs: List[Dict] = field(default_factory=list)
    conditions_met: List[str] = field(default_factory=list)
    target_hold_minutes: int = 15
    scalp_category: str = 'micro'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MicroSnapshot:
    """Real-time intraday market microstructure snapshot."""
    timestamp: str
    btc_price: float
    btc_price_1m_ago: float
    btc_price_3m_ago: float
    btc_price_5m_ago: float
    btc_price_15m_ago: float
    rv_1min: float
    rv_5min: float
    rv_15min: float
    rv_1h: float
    dvol_current: float
    dvol_15m_ago: float
    dvol_1h_ago: float
    iv_rank_15m: float
    bid_depth_btc: float
    ask_depth_btc: float
    book_imbalance: float
    best_bid: float
    best_ask: float
    spread_bps: float
    ret_1m: float
    ret_3m: float
    ret_5m: float
    ret_15m: float
    volume_1m: float
    volume_5m_avg: float
    volume_ratio: float
    atr_1h: float
    atm_call_iv: float
    atm_put_iv: float
    iv_skew: float
    atm_delta: float
    atm_gamma: float
    atm_theta: float
    atm_vega: float
    vov_intraday: float

    def to_dict(self) -> dict:
        return asdict(self)


class DataCache:
    """TTL-based cache for API responses to respect Deribit rate limits."""
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Optional[Any]:
        if key in self._store:
            ts, val = self._store[key]
            if time.time() - ts < ttl:
                return val
        return None

    def set(self, key: str, value: Any):
        self._store[key] = (time.time(), value)

    def clear(self):
        self._store.clear()


class ScalpingEngine:
    """Intraday micro-strategy engine for 3-minute scan cycles."""

    CANDLE_TTL = 60
    ORDERBOOK_TTL = 5
    DVOL_TTL = 30
    TICKER_TTL = 5
    INSTRUMENTS_TTL = 300

    def __init__(self, equity: float = 100.0, max_signals_per_scan: int = 5,
                 scalp_size_pct: float = 0.02):
        self.equity = equity  # In BTC — matches Deribit testnet balance
        self.max_signals_per_scan = max_signals_per_scan
        self.scalp_size_pct = scalp_size_pct  # 2% of equity per scalp trade
        self.client = httpx.Client(timeout=15)
        self.cache = DataCache()
        self._scan_count = 0
        self._recent_signals: Dict[str, float] = {}
        self.SIGNAL_COOLDOWN = 180  # 3 min between same signal type

    # =================================================================
    # DATA FETCHING
    # =================================================================

    def _api_get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        try:
            r = self.client.get(f'{DERIBIT_PUBLIC}{endpoint}', params=params or {})
            data = r.json()
            if 'error' in data:
                print(f'  API ERR ({endpoint}): {data["error"]}')
                return None
            return data.get('result')
        except Exception as e:
            print(f'  API ERR ({endpoint}): {e}')
            return None

    def get_btc_price(self) -> float:
        cached = self.cache.get('btc_price', self.TICKER_TTL)
        if cached is not None:
            return cached
        result = self._api_get('/public/get_index_price', {'index_name': 'btc_usd'})
        price = result.get('index_price', 0) if result else 0
        if price:
            self.cache.set('btc_price', price)
        return price

    def get_1min_candles(self, instrument: str = 'BTC-PERPETUAL',
                         minutes: int = 60) -> List[Dict]:
        cache_key = f'candles_1m_{instrument}_{minutes}'
        cached = self.cache.get(cache_key, self.CANDLE_TTL)
        if cached is not None:
            return cached
        end_ts = int(time.time() * 1000)
        start_ts = end_ts - (minutes * 60 * 1000)
        result = self._api_get('/public/get_tradingview_chart_data', {
            'instrument_name': instrument, 'resolution': '1',
            'start_timestamp': start_ts, 'end_timestamp': end_ts,
        })
        if not result or result.get('status') != 'ok':
            return []
        ticks = result.get('ticks', [])
        opens = result.get('open', [])
        highs = result.get('high', [])
        lows = result.get('low', [])
        closes = result.get('close', [])
        volumes = result.get('volume', [])
        candles = []
        for i in range(len(ticks)):
            candles.append({
                'ts': ticks[i],
                'open': opens[i] if i < len(opens) else 0,
                'high': highs[i] if i < len(highs) else 0,
                'low': lows[i] if i < len(lows) else 0,
                'close': closes[i] if i < len(closes) else 0,
                'volume': volumes[i] if i < len(volumes) else 0,
            })
        self.cache.set(cache_key, candles)
        return candles

    def get_dvol_candles(self, minutes: int = 60) -> List[Dict]:
        cache_key = f'dvol_1m_{minutes}'
        cached = self.cache.get(cache_key, self.DVOL_TTL)
        if cached is not None:
            return cached
        end_ts = int(time.time() * 1000)
        start_ts = end_ts - (minutes * 60 * 1000)
        result = self._api_get('/public/get_volatility_index_data', {
            'currency': 'BTC', 'resolution': 60,
            'start_timestamp': start_ts, 'end_timestamp': end_ts,
        })
        if not result:
            return []
        data = result.get('data', [])
        candles = []
        for entry in data:
            if len(entry) >= 5:
                candles.append({
                    'ts': entry[0],
                    'open': entry[1] / 100.0,
                    'high': entry[2] / 100.0,
                    'low': entry[3] / 100.0,
                    'close': entry[4] / 100.0,
                })
        self.cache.set(cache_key, candles)
        return candles

    def get_order_book(self, instrument: str = 'BTC-PERPETUAL',
                       depth: int = 10) -> Optional[dict]:
        cache_key = f'book_{instrument}_{depth}'
        cached = self.cache.get(cache_key, self.ORDERBOOK_TTL)
        if cached is not None:
            return cached
        result = self._api_get('/public/get_order_book', {
            'instrument_name': instrument, 'depth': depth,
        })
        if result:
            self.cache.set(cache_key, result)
        return result

    def get_ticker(self, instrument: str) -> Optional[dict]:
        cache_key = f'ticker_{instrument}'
        cached = self.cache.get(cache_key, self.TICKER_TTL)
        if cached is not None:
            return cached
        result = self._api_get('/public/ticker', {'instrument_name': instrument})
        if result:
            self.cache.set(cache_key, result)
        return result

    def get_nearest_atm_options(self, btc_price: float) -> Tuple[Optional[str], Optional[str]]:
        cache_key = f'atm_options_{int(btc_price / 100)}'
        cached = self.cache.get(cache_key, self.INSTRUMENTS_TTL)
        if cached is not None:
            return cached
        result = self._api_get('/public/get_instruments', {
            'currency': 'BTC', 'kind': 'option', 'expired': 'false',
        })
        if not result:
            return None, None
        now_ts = time.time() * 1000
        min_expiry = now_ts + 4 * 3600 * 1000
        best_call = best_put = None
        best_cs = best_ps = float('inf')
        for inst in result:
            exp_ts = inst.get('expiration_timestamp', 0)
            if exp_ts < min_expiry:
                continue
            strike = inst.get('strike', 0)
            strike_dist = abs(strike - btc_price)
            time_dist = (exp_ts - now_ts) / (3600 * 1000)
            score = strike_dist / btc_price * 100 + time_dist * 0.1
            name = inst.get('instrument_name', '')
            if inst.get('option_type') == 'call' and score < best_cs:
                best_call, best_cs = name, score
            elif inst.get('option_type') == 'put' and score < best_ps:
                best_put, best_ps = name, score
        pair = (best_call, best_put)
        self.cache.set(cache_key, pair)
        return pair

    # =================================================================
    # INDICATOR COMPUTATION
    # =================================================================

    @staticmethod
    def compute_rv_from_candles(candles: List[Dict], n_minutes: int) -> float:
        if len(candles) < max(n_minutes, 2):
            return 0.0
        recent = candles[-n_minutes:]
        closes = [c['close'] for c in recent if c['close'] > 0]
        if len(closes) < 2:
            return 0.0
        log_ret = [math.log(closes[i] / closes[i-1])
                   for i in range(1, len(closes)) if closes[i-1] > 0]
        if not log_ret:
            return 0.0
        return float(np.std(log_ret, ddof=1)) * math.sqrt(525600)

    @staticmethod
    def compute_atr_from_candles(candles: List[Dict], period: int = 60) -> float:
        if len(candles) < period + 1:
            return 0.0
        recent = candles[-(period + 1):]
        trs = []
        for i in range(1, len(recent)):
            h, l, pc = recent[i]['high'], recent[i]['low'], recent[i-1]['close']
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return float(np.mean(trs)) if trs else 0.0

    @staticmethod
    def compute_vov_from_dvol(dvol_candles: List[Dict], n_minutes: int = 30) -> float:
        if len(dvol_candles) < max(n_minutes, 3):
            return 0.0
        closes = [c['close'] for c in dvol_candles[-n_minutes:] if c['close'] > 0]
        if len(closes) < 3:
            return 0.0
        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        return float(np.std(changes, ddof=1)) if changes else 0.0

    # =================================================================
    # BUILD MICRO SNAPSHOT
    # =================================================================

    def build_micro_snapshot(self) -> Optional[MicroSnapshot]:
        print('\n' + '=' * 60)
        print('SCALPING ENGINE - MICRO SNAPSHOT')
        print('=' * 60)

        btc_price = self.get_btc_price()
        if not btc_price:
            print('  ERROR: Cannot fetch BTC price')
            return None

        candles = self.get_1min_candles('BTC-PERPETUAL', 60)
        if len(candles) < 15:
            while len(candles) < 15:
                candles.insert(0, {
                    'ts': 0, 'open': btc_price, 'high': btc_price,
                    'low': btc_price, 'close': btc_price, 'volume': 0})

        price_1m = candles[-2]['close'] if len(candles) >= 2 else btc_price
        price_3m = candles[-4]['close'] if len(candles) >= 4 else btc_price
        price_5m = candles[-6]['close'] if len(candles) >= 6 else btc_price
        price_15m = candles[-16]['close'] if len(candles) >= 16 else btc_price

        rv_1min = self.compute_rv_from_candles(candles, 2)
        rv_5min = self.compute_rv_from_candles(candles, 5)
        rv_15min = self.compute_rv_from_candles(candles, 15)
        rv_1h = self.compute_rv_from_candles(candles, 60)

        dvol_candles = self.get_dvol_candles(60)
        dvol_current = dvol_candles[-1]['close'] if dvol_candles else 0
        dvol_15m = dvol_candles[-16]['close'] if len(dvol_candles) >= 16 else dvol_current
        dvol_1h = dvol_candles[0]['close'] if dvol_candles else dvol_current

        iv_rank_15m = 0.5
        if len(dvol_candles) >= 15:
            recent_ivs = [c['close'] for c in dvol_candles[-15:]]
            rng = max(recent_ivs) - min(recent_ivs)
            if rng > 0:
                iv_rank_15m = (dvol_current - min(recent_ivs)) / rng

        vov_intraday = self.compute_vov_from_dvol(dvol_candles, 30)

        book = self.get_order_book('BTC-PERPETUAL', 10)
        bid_depth = ask_depth = best_bid = best_ask = spread_bps = 0.0
        if book:
            bids, asks = book.get('bids', []), book.get('asks', [])
            bid_depth = sum(b[1] for b in bids[:5]) if bids else 0
            ask_depth = sum(a[1] for a in asks[:5]) if asks else 0
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            if best_bid and best_ask:
                spread_bps = (best_ask - best_bid) / ((best_bid + best_ask) / 2) * 10000
        book_imbalance = bid_depth / ask_depth if ask_depth > 0 else 1.0

        ret_1m = (btc_price - price_1m) / price_1m if price_1m else 0
        ret_3m = (btc_price - price_3m) / price_3m if price_3m else 0
        ret_5m = (btc_price - price_5m) / price_5m if price_5m else 0
        ret_15m = (btc_price - price_15m) / price_15m if price_15m else 0

        vol_1m = candles[-1]['volume'] if candles else 0
        vol_5m_avg = float(np.mean([c['volume'] for c in candles[-5:]])) if len(candles) >= 5 else vol_1m
        vol_ratio = vol_1m / vol_5m_avg if vol_5m_avg > 0 else 1.0
        atr_1h = self.compute_atr_from_candles(candles, min(60, len(candles) - 1))

        atm_call, atm_put = self.get_nearest_atm_options(btc_price)
        atm_call_iv = atm_put_iv = atm_delta = atm_gamma = atm_theta = atm_vega = 0.0
        if atm_call:
            tc = self.get_ticker(atm_call)
            if tc:
                g = tc.get('greeks', {})
                atm_call_iv = tc.get('mark_iv', 0) / 100.0
                atm_delta = g.get('delta', 0.5)
                atm_gamma = g.get('gamma', 0)
                atm_theta = g.get('theta', 0)
                atm_vega = g.get('vega', 0)
        if atm_put:
            tp = self.get_ticker(atm_put)
            if tp:
                atm_put_iv = tp.get('mark_iv', 0) / 100.0
        iv_skew = atm_put_iv - atm_call_iv

        snap = MicroSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            btc_price=btc_price, btc_price_1m_ago=price_1m,
            btc_price_3m_ago=price_3m, btc_price_5m_ago=price_5m,
            btc_price_15m_ago=price_15m,
            rv_1min=round(rv_1min, 4), rv_5min=round(rv_5min, 4),
            rv_15min=round(rv_15min, 4), rv_1h=round(rv_1h, 4),
            dvol_current=round(dvol_current, 4),
            dvol_15m_ago=round(dvol_15m, 4),
            dvol_1h_ago=round(dvol_1h, 4),
            iv_rank_15m=round(iv_rank_15m, 4),
            bid_depth_btc=round(bid_depth, 2),
            ask_depth_btc=round(ask_depth, 2),
            book_imbalance=round(book_imbalance, 4),
            best_bid=best_bid, best_ask=best_ask,
            spread_bps=round(spread_bps, 2),
            ret_1m=round(ret_1m, 6), ret_3m=round(ret_3m, 6),
            ret_5m=round(ret_5m, 6), ret_15m=round(ret_15m, 6),
            volume_1m=vol_1m, volume_5m_avg=round(vol_5m_avg, 2),
            volume_ratio=round(vol_ratio, 4),
            atr_1h=round(atr_1h, 2),
            atm_call_iv=round(atm_call_iv, 4),
            atm_put_iv=round(atm_put_iv, 4),
            iv_skew=round(iv_skew, 4),
            atm_delta=round(atm_delta, 4),
            atm_gamma=round(atm_gamma, 6),
            atm_theta=round(atm_theta, 6),
            atm_vega=round(atm_vega, 6),
            vov_intraday=round(vov_intraday, 6),
        )

        print(f'  BTC: ${btc_price:,.2f}  |  DVOL: {dvol_current*100:.1f}%')
        print(f'  RV 1m/5m/1h: {rv_1min*100:.1f}%/{rv_5min*100:.1f}%/{rv_1h*100:.1f}%')
        print(f'  Book imb: {book_imbalance:.2f} | Spread: {spread_bps:.1f}bps')
        print(f'  Ret(3m): {ret_3m*100:+.3f}% | Vol ratio: {vol_ratio:.2f}')
        print(f'  Skew: {iv_skew*100:+.2f}% | ATR(1h): ${atr_1h:,.0f}')
        return snap

    # =================================================================
    # COOLDOWN
    # =================================================================

    def _check_cooldown(self, key: str) -> bool:
        last = self._recent_signals.get(key, 0)
        return (time.time() - last) < self.SIGNAL_COOLDOWN

    def _mark_signal(self, key: str):
        self._recent_signals[key] = time.time()
        cutoff = time.time() - 600
        self._recent_signals = {k: v for k, v in self._recent_signals.items() if v > cutoff}

    # =================================================================
    # STRATEGY E: GAMMA SCALP
    # =================================================================

    def check_strategy_e(self, snap: MicroSnapshot) -> Optional[ScalpSignal]:
        """E: Gamma Scalp - Buy ATM straddle when short-term RV spikes.
        1min RV > 5min RV by 20%+, VoV elevated, spread < 50bps.
        """
        if self._check_cooldown('E_GammaScalp'):
            return None
        conditions = []
        if snap.rv_5min <= 0 or snap.rv_1min <= 0:
            return None
        rv_ratio = snap.rv_1min / snap.rv_5min if snap.rv_5min > 0 else 0
        if rv_ratio < 1.20:
            return None
        conditions.append(f'RV spike: 1m={snap.rv_1min*100:.1f}% vs 5m={snap.rv_5min*100:.1f}% (ratio={rv_ratio:.2f})')
        if snap.vov_intraday < 0.001:
            return None
        conditions.append(f'VoV elevated: {snap.vov_intraday:.6f}')
        if snap.spread_bps > 50:
            return None
        conditions.append(f'Spread OK: {snap.spread_bps:.1f} bps')
        conf_score = min(1.0, 0.4 + (rv_ratio - 1.2) * 0.5 + snap.atm_gamma * 1000)
        conf_label = 'HIGH' if conf_score > 0.7 else 'MEDIUM' if conf_score > 0.5 else 'LOW'
        size = round(self.equity * self.scalp_size_pct, 6)
        strike = round(snap.btc_price / 1000) * 1000
        self._mark_signal('E_GammaScalp')
        return ScalpSignal(
            strategy_name='E_GammaScalp',
            signal_type='long_straddle', direction='long',
            description=f'Gamma scalp: buy ATM straddle @ {strike:,.0f}, RV spike {rv_ratio:.2f}x',
            confidence=conf_label, confidence_score=round(conf_score, 4),
            suggested_instrument=f'BTC-{strike:.0f}-Straddle',
            suggested_strike=strike,
            suggested_expiry=(datetime.now(timezone.utc) + timedelta(days=2)).strftime('%Y-%m-%d'),
            option_type='straddle',
            suggested_size_btc=round(size, 6),
            max_loss_usd=round(size * snap.btc_price * snap.dvol_current * 0.1, 2),
            legs=[
                {'type': 'call', 'strike': strike, 'direction': 'buy'},
                {'type': 'put', 'strike': strike, 'direction': 'buy'},
            ],
            conditions_met=conditions, target_hold_minutes=15, scalp_category='micro',
        )

    # =================================================================
    # STRATEGY F: IV CRUSH SCALP
    # =================================================================

    def check_strategy_f(self, snap: MicroSnapshot) -> Optional[ScalpSignal]:
        """F: IV Crush Scalp - Sell ATM when IV rank spikes >80th pctl.
        DVOL increased vs 15min ago, RV is NOT accelerating.
        """
        if self._check_cooldown('F_IVCrush'):
            return None
        conditions = []
        if snap.iv_rank_15m < 0.80:
            return None
        conditions.append(f'IV rank (15m): {snap.iv_rank_15m*100:.0f}% >= 80%')
        if snap.dvol_current <= 0 or snap.dvol_15m_ago <= 0:
            return None
        dvol_change = (snap.dvol_current - snap.dvol_15m_ago) / snap.dvol_15m_ago
        if dvol_change < 0.02:
            return None
        conditions.append(f'DVOL spike: {dvol_change*100:+.1f}% vs 15m ago')
        if snap.rv_5min > 0 and snap.rv_1min > snap.rv_5min * 1.3:
            return None
        rv_txt = f'1m/5m={snap.rv_1min/snap.rv_5min:.2f}' if snap.rv_5min > 0 else 'n/a'
        conditions.append(f'RV stable: {rv_txt}')
        conf_score = min(1.0, 0.5 + snap.iv_rank_15m * 0.3 + dvol_change * 2)
        conf_label = 'HIGH' if conf_score > 0.7 else 'MEDIUM'
        size = round(self.equity * self.scalp_size_pct * 0.8, 6)
        strike = round(snap.btc_price / 1000) * 1000
        self._mark_signal('F_IVCrush')
        return ScalpSignal(
            strategy_name='F_IVCrush_Scalp',
            signal_type='short_straddle', direction='short',
            description=f'IV crush: sell ATM straddle @ {strike:,.0f}, DVOL +{dvol_change*100:.1f}%',
            confidence=conf_label, confidence_score=round(conf_score, 4),
            suggested_instrument=f'BTC-{strike:.0f}-Straddle',
            suggested_strike=strike,
            suggested_expiry=(datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d'),
            option_type='straddle',
            suggested_size_btc=round(size, 6),
            max_loss_usd=round(size * snap.btc_price * snap.dvol_current * 0.3, 2),
            legs=[
                {'type': 'call', 'strike': strike, 'direction': 'sell'},
                {'type': 'put', 'strike': strike, 'direction': 'sell'},
            ],
            conditions_met=conditions, target_hold_minutes=30, scalp_category='micro',
        )

    # =================================================================
    # STRATEGY G: ORDER BOOK IMBALANCE
    # =================================================================

    def check_strategy_g(self, snap: MicroSnapshot) -> Optional[ScalpSignal]:
        """G: Order Book Imbalance - Directional OTM from depth asymmetry.
        Bid/ask ratio >1.5 -> buy call, <0.67 -> buy put.
        """
        if self._check_cooldown('G_BookImbalance'):
            return None
        conditions = []
        if snap.bid_depth_btc <= 0 or snap.ask_depth_btc <= 0:
            return None
        bullish = snap.book_imbalance > 1.5
        bearish = snap.book_imbalance < 0.67
        if not bullish and not bearish:
            return None
        vol_confirmed = snap.volume_ratio > 1.3
        if bullish:
            conditions.append(f'Bid-heavy: imb={snap.book_imbalance:.2f}')
            opt_type, strike = 'call', round(snap.btc_price * 1.02 / 1000) * 1000
            desc_dir = 'bullish'
        else:
            conditions.append(f'Ask-heavy: imb={snap.book_imbalance:.2f}')
            opt_type, strike = 'put', round(snap.btc_price * 0.98 / 1000) * 1000
            desc_dir = 'bearish'
        if vol_confirmed:
            conditions.append(f'Volume spike: {snap.volume_ratio:.2f}x')
        conf_score = 0.4 + min(0.3, (abs(snap.book_imbalance - 1.0) - 0.5) * 0.6)
        if vol_confirmed:
            conf_score += 0.2
        conf_score = min(1.0, conf_score)
        conf_label = 'HIGH' if conf_score > 0.7 else 'MEDIUM' if conf_score > 0.5 else 'LOW'
        size = round(self.equity * self.scalp_size_pct * 0.6, 6)
        self._mark_signal('G_BookImbalance')
        return ScalpSignal(
            strategy_name='G_BookImbalance',
            signal_type=f'long_otm_{opt_type}', direction='long',
            description=f'Book imbalance {desc_dir}: buy {opt_type.upper()} @ {strike:,.0f}',
            confidence=conf_label, confidence_score=round(conf_score, 4),
            suggested_instrument=f'BTC-{strike:.0f}-{opt_type[0].upper()}',
            suggested_strike=strike,
            suggested_expiry=(datetime.now(timezone.utc) + timedelta(days=5)).strftime('%Y-%m-%d'),
            option_type=opt_type,
            suggested_size_btc=round(size, 6),
            max_loss_usd=round(size * snap.btc_price * 0.02, 2),
            legs=[{'type': opt_type, 'strike': strike, 'direction': 'buy'}],
            conditions_met=conditions, target_hold_minutes=20, scalp_category='micro',
        )

    # =================================================================
    # STRATEGY H: MICRO-MOMENTUM
    # =================================================================

    def check_strategy_h(self, snap: MicroSnapshot) -> Optional[ScalpSignal]:
        """H: Micro-Momentum - Continuation on 3-min breakout vs ATR.
        3min |return| > 0.5*ATR_1h, volume ratio > 1.5.
        """
        if self._check_cooldown('H_MicroMomentum'):
            return None
        conditions = []
        if snap.atr_1h <= 0:
            return None
        abs_3m_move = abs(snap.ret_3m) * snap.btc_price
        threshold = snap.atr_1h * 0.5
        if abs_3m_move < threshold:
            return None
        conditions.append(f'3m move: ${abs_3m_move:,.0f} > 0.5*ATR(${threshold:,.0f})')
        if snap.volume_ratio < 1.5:
            return None
        conditions.append(f'Volume spike: {snap.volume_ratio:.2f}x avg')
        bullish = snap.ret_3m > 0
        if bullish:
            opt_type = 'call'
            strike = round(snap.btc_price * 1.015 / 1000) * 1000
            conditions.append(f'Bullish: +{snap.ret_3m*100:.3f}% in 3min')
        else:
            opt_type = 'put'
            strike = round(snap.btc_price * 0.985 / 1000) * 1000
            conditions.append(f'Bearish: {snap.ret_3m*100:.3f}% in 3min')
        move_strength = abs_3m_move / threshold
        conf_score = min(1.0, 0.4 + move_strength * 0.2 + (snap.volume_ratio - 1.5) * 0.15)
        conf_label = 'HIGH' if conf_score > 0.7 else 'MEDIUM' if conf_score > 0.5 else 'LOW'
        size = round(self.equity * self.scalp_size_pct * 0.7, 6)
        self._mark_signal('H_MicroMomentum')
        return ScalpSignal(
            strategy_name='H_MicroMomentum',
            signal_type=f'long_otm_{opt_type}', direction='long',
            description=f'Micro-momentum: buy {opt_type.upper()} @ {strike:,.0f}, 3m ${abs_3m_move:,.0f}',
            confidence=conf_label, confidence_score=round(conf_score, 4),
            suggested_instrument=f'BTC-{strike:.0f}-{opt_type[0].upper()}',
            suggested_strike=strike,
            suggested_expiry=(datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d'),
            option_type=opt_type,
            suggested_size_btc=round(size, 6),
            max_loss_usd=round(size * snap.btc_price * 0.015, 2),
            legs=[{'type': opt_type, 'strike': strike, 'direction': 'buy'}],
            conditions_met=conditions, target_hold_minutes=10, scalp_category='micro',
        )

    # =================================================================
    # STRATEGY I: SKEW ARBITRAGE
    # =================================================================

    def check_strategy_i(self, snap: MicroSnapshot) -> Optional[ScalpSignal]:
        """I: Skew Arbitrage - Risk reversal when put/call IV diverges >5%.
        Puts rich -> buy call + sell put. Calls rich -> buy put + sell call.
        """
        if self._check_cooldown('I_SkewArb'):
            return None
        conditions = []
        if snap.atm_call_iv <= 0 or snap.atm_put_iv <= 0:
            return None
        skew_pct = snap.iv_skew
        abs_skew = abs(skew_pct)
        if abs_skew < 0.05:
            return None
        strike = round(snap.btc_price / 1000) * 1000
        if skew_pct > 0.05:
            conditions.append(f'Put skew: {skew_pct*100:+.1f}% (put IV={snap.atm_put_iv*100:.1f}%)')
            legs = [
                {'type': 'call', 'strike': strike, 'direction': 'buy'},
                {'type': 'put', 'strike': strike, 'direction': 'sell'},
            ]
            sig_type, direction = 'risk_reversal_bullish', 'long'
            desc = f'Bullish RR @ {strike:,.0f}: buy C + sell P, skew={skew_pct*100:+.1f}%'
        else:
            conditions.append(f'Call skew: {-skew_pct*100:+.1f}% (call IV={snap.atm_call_iv*100:.1f}%)')
            legs = [
                {'type': 'put', 'strike': strike, 'direction': 'buy'},
                {'type': 'call', 'strike': strike, 'direction': 'sell'},
            ]
            sig_type, direction = 'risk_reversal_bearish', 'short'
            desc = f'Bearish RR @ {strike:,.0f}: buy P + sell C, skew={skew_pct*100:+.1f}%'
        conf_score = min(1.0, 0.4 + abs_skew * 3)
        conf_label = 'HIGH' if conf_score > 0.7 else 'MEDIUM' if conf_score > 0.5 else 'LOW'
        size = round(self.equity * self.scalp_size_pct * 0.5, 6)
        self._mark_signal('I_SkewArb')
        return ScalpSignal(
            strategy_name='I_SkewArbitrage',
            signal_type=sig_type, direction=direction,
            description=desc,
            confidence=conf_label, confidence_score=round(conf_score, 4),
            suggested_instrument=f'BTC-{strike:.0f}-RiskReversal',
            suggested_strike=strike,
            suggested_expiry=(datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d'),
            option_type='straddle',
            suggested_size_btc=round(size, 6),
            max_loss_usd=round(size * snap.btc_price * 0.05, 2),
            legs=legs,
            conditions_met=conditions, target_hold_minutes=30, scalp_category='micro',
        )

    # =================================================================
    # FULL SCAN
    # =================================================================

    def scan(self) -> Tuple[Optional[MicroSnapshot], List[ScalpSignal]]:
        """Run all 5 scalping strategies against current micro snapshot."""
        self._scan_count += 1
        print(f'\nSCALP SCAN #{self._scan_count}')
        snapshot = self.build_micro_snapshot()
        if not snapshot:
            return None, []
        signals: List[ScalpSignal] = []
        strategy_checks = [
            ('E', self.check_strategy_e),
            ('F', self.check_strategy_f),
            ('G', self.check_strategy_g),
            ('H', self.check_strategy_h),
            ('I', self.check_strategy_i),
        ]
        for name, check_fn in strategy_checks:
            try:
                sig = check_fn(snapshot)
                if sig:
                    signals.append(sig)
                    print(f'  [SCALP SIGNAL] {name}: {sig.description}')
                else:
                    print(f'  {name}: No signal')
            except Exception as e:
                print(f'  {name} ERROR: {e}')
        if len(signals) > self.max_signals_per_scan:
            signals.sort(key=lambda s: s.confidence_score, reverse=True)
            signals = signals[:self.max_signals_per_scan]
        print(f'\nSCALP SCAN COMPLETE: {len(signals)} signal(s)')
        return snapshot, signals
