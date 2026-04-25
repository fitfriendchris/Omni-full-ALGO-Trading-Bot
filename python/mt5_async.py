"""
mt5_async.py — Async MT5 connector (shadow module, not yet active).

Shadow module for future replacement of JSON file exchange.
Currently NOT used by auto_trader.py — run in parallel on paper
to validate before cutover.

Usage (future):
    from mt5_async import MT5Async
    conn = MT5Async()
    await conn.connect()
    account = await conn.get_account()
    positions = await conn.get_positions()
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    mt5 = None

try:
    from config import cfg
    _MAGIC = cfg.MAGIC
    _PAPER_MODE = cfg.PAPER_MODE
    _TRADE_SYMBOLS = cfg.TRADE_SYMBOLS
except Exception:
    _MAGIC = 20250411
    _PAPER_MODE = True
    _TRADE_SYMBOLS = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]

log = logging.getLogger("MT5Async")

# ── Timeframe mapping ─────────────────────────────────────────────────────────
# Values are seconds (used as fallback); MT5 constants are preferred at runtime.
TF_MAP = {
    "M1":  1,
    "M5":  5,
    "M15": 15,
    "M30": 30,
    "H1":  60 * 60,
    "H4":  4 * 60 * 60,
    "D1":  24 * 60 * 60,
    "W1":  7 * 24 * 60 * 60,
}


def _mt5_timeframe(tf_str: str):
    """Return the MT5 TIMEFRAME_* constant for a given string, or None."""
    if not MT5_AVAILABLE:
        return None
    mapping = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
        "W1":  mt5.TIMEFRAME_W1,
    }
    return mapping.get(tf_str.upper())


# ── Order-type helpers ────────────────────────────────────────────────────────

def _order_type_const(direction: str, order_type: str):
    """
    Resolve (direction, order_type) to an mt5.ORDER_TYPE_* constant.
    direction: "BUY" | "SELL"
    order_type: "MARKET" | "LIMIT" | "STOP"
    Returns None if MT5 not available.
    """
    if not MT5_AVAILABLE:
        return None
    key = f"{direction.upper()}_{order_type.upper()}"
    mapping = {
        "BUY_MARKET":  mt5.ORDER_TYPE_BUY,
        "SELL_MARKET": mt5.ORDER_TYPE_SELL,
        "BUY_LIMIT":   mt5.ORDER_TYPE_BUY_LIMIT,
        "SELL_LIMIT":  mt5.ORDER_TYPE_SELL_LIMIT,
        "BUY_STOP":    mt5.ORDER_TYPE_BUY_STOP,
        "SELL_STOP":   mt5.ORDER_TYPE_SELL_STOP,
    }
    return mapping.get(key)


# ═════════════════════════════════════════════════════════════════════════════
# MT5Async
# ═════════════════════════════════════════════════════════════════════════════

class MT5Async:
    """
    Async wrapper around the synchronous MetaTrader5 Python library.

    All public methods are async.  Synchronous MT5 calls are dispatched
    to the default ThreadPoolExecutor via run_in_executor so they do not
    block the event loop.

    Paper mode:
        If cfg.PAPER_MODE is True (or MT5 is unavailable), place_order
        returns a synthetic ticket rather than touching the broker.

    Auto-reconnect:
        Any method that calls _ensure_connected() will attempt up to
        MAX_RECONNECT_ATTEMPTS reconnections with RECONNECT_DELAY_SECS
        between tries before returning a safe default.
    """

    MAX_RECONNECT_ATTEMPTS: int = 3
    RECONNECT_DELAY_SECS: float = 2.0

    def __init__(self) -> None:
        self._connected: bool = False
        self._magic: int = _MAGIC
        self._paper_mode: bool = _PAPER_MODE
        self._lock = asyncio.Lock()

    # ── Connection state ──────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True when MT5 terminal is initialised and reachable."""
        return self._connected

    async def _check_alive(self) -> bool:
        """Non-blocking ping: ask MT5 for terminal_info; returns bool."""
        if not MT5_AVAILABLE:
            return False
        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, mt5.terminal_info)
            return info is not None
        except Exception:
            return False

    async def _ensure_connected(self) -> bool:
        """
        Verify connection is live; reconnect up to MAX_RECONNECT_ATTEMPTS
        times on failure.  Returns True if connected after the check.
        """
        if self._connected and await self._check_alive():
            return True

        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            log.info(f"MT5Async reconnect attempt {attempt}/{self.MAX_RECONNECT_ATTEMPTS}")
            success = await self.connect()
            if success:
                return True
            if attempt < self.MAX_RECONNECT_ATTEMPTS:
                await asyncio.sleep(self.RECONNECT_DELAY_SECS)

        log.error("MT5Async: all reconnect attempts failed")
        self._connected = False
        return False

    # ── Public async API ──────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """
        Initialise MT5 terminal connection.
        Returns True on success, False on failure.
        """
        if not MT5_AVAILABLE:
            log.warning("MT5Async.connect: MetaTrader5 package not installed — running in stub mode")
            self._connected = False
            return False

        loop = asyncio.get_event_loop()
        async with self._lock:
            try:
                ok = await loop.run_in_executor(None, mt5.initialize)
                if ok:
                    self._connected = True
                    info = await loop.run_in_executor(None, mt5.terminal_info)
                    log.info(
                        f"MT5Async connected | build={getattr(info, 'build', '?')} "
                        f"| path={getattr(info, 'path', '?')}"
                    )
                else:
                    err = mt5.last_error()
                    log.error(f"MT5Async.connect failed: {err}")
                    self._connected = False
                return ok
            except Exception as exc:
                log.exception(f"MT5Async.connect exception: {exc}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Cleanly shut down the MT5 terminal connection."""
        if not MT5_AVAILABLE:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, mt5.shutdown)
            log.info("MT5Async disconnected")
        except Exception as exc:
            log.warning(f"MT5Async.disconnect exception: {exc}")
        finally:
            self._connected = False

    async def get_account(self) -> dict:
        """
        Return account summary dict matching the existing mt5_connector format:
        {balance, equity, margin, margin_free, currency, leverage}
        Returns empty dict on failure.
        """
        if not MT5_AVAILABLE:
            return {}
        if not await self._ensure_connected():
            return {}

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, mt5.account_info)
            if info is None:
                log.warning(f"MT5Async.get_account: None returned, error={mt5.last_error()}")
                return {}
            return {
                "balance":     float(info.balance),
                "equity":      float(info.equity),
                "margin":      float(info.margin),
                "margin_free": float(info.margin_free),
                "currency":    str(info.currency),
                "leverage":    int(info.leverage),
            }
        except Exception as exc:
            log.exception(f"MT5Async.get_account exception: {exc}")
            return {}

    async def get_positions(self) -> list:
        """
        Return list of open position dicts matching existing position format:
        [{ticket, symbol, type, volume, open_price, current_price,
          sl, tp, profit, swap, magic, comment}]
        Returns empty list on failure.
        """
        if not MT5_AVAILABLE:
            return []
        if not await self._ensure_connected():
            return []

        loop = asyncio.get_event_loop()
        try:
            positions = await loop.run_in_executor(None, mt5.positions_get)
            if positions is None:
                return []
            result = []
            for p in positions:
                result.append({
                    "ticket":        int(p.ticket),
                    "symbol":        str(p.symbol),
                    "type":          "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                    "volume":        float(p.volume),
                    "open_price":    float(p.price_open),
                    "current_price": float(p.price_current),
                    "sl":            float(p.sl),
                    "tp":            float(p.tp),
                    "profit":        float(p.profit),
                    "swap":          float(p.swap),
                    "magic":         int(p.magic),
                    "comment":       str(p.comment),
                })
            return result
        except Exception as exc:
            log.exception(f"MT5Async.get_positions exception: {exc}")
            return []

    async def get_symbol_prices(self, symbols: list) -> dict:
        """
        Return {symbol: {bid, ask, spread, digits}} for each requested symbol.
        Missing symbols are silently omitted.
        Returns empty dict on failure.
        """
        if not MT5_AVAILABLE:
            return {}
        if not await self._ensure_connected():
            return {}

        loop = asyncio.get_event_loop()
        result = {}
        for symbol in symbols:
            try:
                tick = await loop.run_in_executor(None, mt5.symbol_info_tick, symbol)
                sym_info = await loop.run_in_executor(None, mt5.symbol_info, symbol)
                if tick is None or sym_info is None:
                    log.debug(f"MT5Async.get_symbol_prices: no data for {symbol}")
                    continue
                digits = int(sym_info.digits)
                spread = round(float(tick.ask - tick.bid), digits)
                result[symbol] = {
                    "bid":    float(tick.bid),
                    "ask":    float(tick.ask),
                    "spread": spread,
                    "digits": digits,
                }
            except Exception as exc:
                log.warning(f"MT5Async.get_symbol_prices({symbol}) exception: {exc}")
        return result

    async def get_bars(self, symbol: str, timeframe: str, n: int = 200) -> list:
        """
        Return up to n recent OHLCV bars.
        Format: [{time, o, h, l, c, v}]  — compact keys matching existing bar format.
        Returns empty list on failure.
        """
        if not MT5_AVAILABLE:
            return []
        if not await self._ensure_connected():
            return []

        tf_const = _mt5_timeframe(timeframe)
        if tf_const is None:
            log.warning(f"MT5Async.get_bars: unknown timeframe '{timeframe}'")
            return []

        loop = asyncio.get_event_loop()
        try:
            rates = await loop.run_in_executor(
                None,
                lambda: mt5.copy_rates_from_pos(symbol, tf_const, 0, n)
            )
            if rates is None or len(rates) == 0:
                log.debug(f"MT5Async.get_bars: no rates for {symbol}/{timeframe}")
                return []
            result = []
            for r in rates:
                # r is a numpy void / structured array row
                result.append({
                    "time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(),
                    "o":    float(r["open"]),
                    "h":    float(r["high"]),
                    "l":    float(r["low"]),
                    "c":    float(r["close"]),
                    "v":    int(r["tick_volume"]),
                })
            return result
        except Exception as exc:
            log.exception(f"MT5Async.get_bars({symbol},{timeframe}) exception: {exc}")
            return []

    async def place_order(
        self,
        symbol: str,
        direction: str,
        order_type: str,
        price: float,
        sl: float,
        tp: float,
        volume: float,
        comment: str = "",
    ) -> str:
        """
        Place a new order.

        Returns:
          "OK|<ticket>|<volume>"       on success
          "PAPER|SIM_<ts>|<volume>"    in paper mode (no real order sent)
          "ERROR|<reason>"             on failure

        direction:  "BUY"  | "SELL"
        order_type: "MARKET" | "LIMIT" | "STOP"
        """
        if self._paper_mode:
            sim_ticket = f"SIM_{int(time.time())}"
            log.info(
                f"PAPER place_order: {direction} {volume} {symbol} @ {price} "
                f"SL={sl} TP={tp} → {sim_ticket}"
            )
            return f"PAPER|{sim_ticket}|{volume}"

        if not MT5_AVAILABLE:
            return "ERROR|MetaTrader5 not installed"
        if not await self._ensure_connected():
            return "ERROR|not connected"

        ot = _order_type_const(direction, order_type)
        if ot is None:
            return f"ERROR|unknown order type {direction}/{order_type}"

        # For market orders use current tick price; for pending use supplied price.
        if order_type.upper() == "MARKET":
            filling = mt5.ORDER_FILLING_FOK
        else:
            filling = mt5.ORDER_FILLING_RETURN

        request = {
            "action":       mt5.TRADE_ACTION_DEAL if order_type.upper() == "MARKET"
                            else mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       float(volume),
            "type":         ot,
            "price":        float(price),
            "sl":           float(sl),
            "tp":           float(tp),
            "magic":        self._magic,
            "comment":      comment[:31],   # MT5 limit
            "type_filling": filling,
            "type_time":    mt5.ORDER_TIME_GTC,
        }

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: mt5.order_send(request)
            )
            if result is None:
                err = mt5.last_error()
                log.error(f"MT5Async.place_order: order_send returned None, error={err}")
                return f"ERROR|order_send returned None ({err})"
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    f"MT5Async.place_order OK: ticket={result.order} vol={result.volume}"
                )
                return f"OK|{result.order}|{result.volume}"
            else:
                log.warning(
                    f"MT5Async.place_order FAILED: retcode={result.retcode} "
                    f"comment={result.comment}"
                )
                return f"ERROR|retcode={result.retcode} {result.comment}"
        except Exception as exc:
            log.exception(f"MT5Async.place_order exception: {exc}")
            return f"ERROR|exception: {exc}"

    async def close_position(self, ticket: int, volume: float = 0.0) -> str:
        """
        Close an open position by ticket.
        If volume == 0 or omitted, closes the full position.

        Returns "OK" or "ERROR|reason".
        """
        if not MT5_AVAILABLE:
            return "ERROR|MetaTrader5 not installed"
        if not await self._ensure_connected():
            return "ERROR|not connected"

        loop = asyncio.get_event_loop()
        try:
            # Fetch the position to get current details
            positions = await loop.run_in_executor(
                None, lambda: mt5.positions_get(ticket=ticket)
            )
            if not positions:
                return f"ERROR|ticket {ticket} not found"
            pos = positions[0]

            close_vol = float(volume) if volume > 0 else float(pos.volume)
            close_type = (
                mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )

            # Get current price for the close
            tick = await loop.run_in_executor(
                None, lambda: mt5.symbol_info_tick(pos.symbol)
            )
            if tick is None:
                return f"ERROR|no tick for {pos.symbol}"
            close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       close_vol,
                "type":         close_type,
                "position":     ticket,
                "price":        close_price,
                "magic":        self._magic,
                "comment":      "close",
                "type_filling": mt5.ORDER_FILLING_FOK,
            }

            result = await loop.run_in_executor(
                None, lambda: mt5.order_send(request)
            )
            if result is None:
                return f"ERROR|order_send None ({mt5.last_error()})"
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"MT5Async.close_position OK: ticket={ticket}")
                return "OK"
            return f"ERROR|retcode={result.retcode} {result.comment}"
        except Exception as exc:
            log.exception(f"MT5Async.close_position exception: {exc}")
            return f"ERROR|exception: {exc}"

    async def modify_position(self, ticket: int, sl: float, tp: float) -> str:
        """
        Modify the SL/TP of an open position.

        Returns "OK" or "ERROR|reason".
        """
        if not MT5_AVAILABLE:
            return "ERROR|MetaTrader5 not installed"
        if not await self._ensure_connected():
            return "ERROR|not connected"

        loop = asyncio.get_event_loop()
        try:
            request = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "sl":       float(sl),
                "tp":       float(tp),
            }
            result = await loop.run_in_executor(
                None, lambda: mt5.order_send(request)
            )
            if result is None:
                return f"ERROR|order_send None ({mt5.last_error()})"
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"MT5Async.modify_position OK: ticket={ticket} sl={sl} tp={tp}")
                return "OK"
            return f"ERROR|retcode={result.retcode} {result.comment}"
        except Exception as exc:
            log.exception(f"MT5Async.modify_position exception: {exc}")
            return f"ERROR|exception: {exc}"

    async def get_trade_history(self, days: int = 7) -> list:
        """
        Return a list of closed trade dicts for the last `days` days.
        Each dict: {ticket, symbol, type, volume, open_price, close_price,
                    open_time, close_time, profit, swap, commission, comment, magic}
        Returns empty list on failure.
        """
        if not MT5_AVAILABLE:
            return []
        if not await self._ensure_connected():
            return []

        import datetime as _dt
        date_from = _dt.datetime.now(tz=timezone.utc) - _dt.timedelta(days=days)
        date_to   = _dt.datetime.now(tz=timezone.utc)

        loop = asyncio.get_event_loop()
        try:
            deals = await loop.run_in_executor(
                None,
                lambda: mt5.history_deals_get(date_from, date_to)
            )
            if deals is None:
                return []
            result = []
            for d in deals:
                # Skip non-trade deals (deposits, withdrawals, etc.)
                if d.entry not in (mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_OUT,
                                   mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY):
                    continue
                result.append({
                    "ticket":      int(d.ticket),
                    "order":       int(d.order),
                    "symbol":      str(d.symbol),
                    "type":        "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                    "entry":       int(d.entry),
                    "volume":      float(d.volume),
                    "open_price":  float(d.price),
                    "close_price": float(d.price),   # deal price IS the exec price
                    "open_time":   datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                    "close_time":  datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                    "profit":      float(d.profit),
                    "swap":        float(d.swap),
                    "commission":  float(d.commission),
                    "comment":     str(d.comment),
                    "magic":       int(d.magic),
                })
            return result
        except Exception as exc:
            log.exception(f"MT5Async.get_trade_history exception: {exc}")
            return []

    # ── Context manager support ───────────────────────────────────────────────

    async def __aenter__(self) -> "MT5Async":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()


# ── Module-level convenience instance (lazy) ──────────────────────────────────
# Import and use directly:
#   from mt5_async import connector
#   await connector.connect()
connector: Optional[MT5Async] = None


def get_connector() -> MT5Async:
    """Return (and lazily create) the module-level MT5Async singleton."""
    global connector
    if connector is None:
        connector = MT5Async()
    return connector
