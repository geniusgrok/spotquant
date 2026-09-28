"""One continuous spot account. The decision is spotquant.model.Model.

Long or cash, no borrow, no short, no futures. A bullish completed daily close
buys at the next daily open. While long, a continuous trailing stop (the spot
``trailingDelta`` order, at most 20%) is modeled on the daily range by taking
the high before the low. The SMA exit sells at the next open and cancels that
trail. Costs and the CNY conversion are applied here.
"""
from __future__ import annotations

from decimal import Decimal as D

from spotquant.model import DAY, Model

CONVERSION = D('0.001')
FEE = D('0.001')
ENTRY_SLIP = D('0.0005')
EXIT_SLIP = D('0.0005')
STOP_SLIP = D('0.001')
INITIAL_CNY = D('10000')
YEAR_MS = D('31556952000')  # 365.2425 * 86400 * 1000


class Book:
    def __init__(self, fx, start_ms: int, *, fee=FEE, entry_slip=ENTRY_SLIP, exit_slip=EXIT_SLIP,
                 stop_slip=STOP_SLIP, conversion=CONVERSION):
        self.fx = fx
        self.fee = D(fee)
        self.entry_slip = D(entry_slip)
        self.exit_slip = D(exit_slip)
        self.stop_slip = D(stop_slip)
        self.conversion = D(conversion)
        self.usdt = (INITIAL_CNY / fx(start_ms)) * (D(1) - self.conversion)
        self.btc = D(0)
        self.fees = D(0)
        self.peak_cny = self._cny(self.usdt, start_ms)
        self.mdd = D(0)
        self.mdd_at = start_ms
        self.peak_high: D | None = None
        self.trades: list[dict] = []
        self._entry_px = D(0)
        self._entry_ms = 0

    def _cny(self, usdt_equity: D, now_ms: int) -> D:
        return usdt_equity * self.fx(now_ms) * (D(1) - self.conversion)

    def mark(self, usdt_equity: D, now_ms: int, *, adverse: bool):
        cny = self._cny(usdt_equity, now_ms)
        if cny > self.peak_cny:
            self.peak_cny = cny
        if adverse and self.peak_cny > 0:
            drawdown = D(1) - cny / self.peak_cny
            if drawdown > self.mdd:
                self.mdd = drawdown
                self.mdd_at = now_ms

    def equity_usdt(self, price: D) -> D:
        return self.usdt + self.btc * price

    def buy(self, price: D, now_ms: int):
        fill = price * (D(1) + self.entry_slip)
        spent = self.usdt
        fee = spent * self.fee
        self.fees += fee
        self.btc = (spent - fee) / fill
        self.usdt = D(0)
        self._entry_px = fill
        self._entry_ms = now_ms

    def sell(self, price: D, now_ms: int, kind: str, slip: D):
        fill = price * (D(1) - slip)
        gross = self.btc * fill
        fee = gross * self.fee
        self.fees += fee
        self.usdt = gross - fee
        self.trades.append({
            'entry_ms': self._entry_ms,
            'exit_ms': now_ms,
            'entry': format(self._entry_px, 'f'),
            'exit': format(fill, 'f'),
            'kind': kind,
        })
        self.btc = D(0)
        self.peak_high = None


def _trail(book: Book, model: Model, open_ms: int, open_: D, high: D, low: D) -> bool:
    """Continuous trailing stop on one daily range. True when it sells.

    The high tightens the stop before the low is tested. A gap through the
    stop inherited from prior days sells at the open.
    """
    stop = model.stop_price(book.peak_high)
    if open_ <= stop:
        book.sell(open_, open_ms, 'gap', book.stop_slip)
        book.mark(book.usdt, open_ms, adverse=True)
        return True
    if high > book.peak_high:
        book.peak_high = high
        stop = model.stop_price(book.peak_high)
    book.mark(book.equity_usdt(high), open_ms, adverse=False)
    if low <= stop:
        book.sell(stop, open_ms, 'trail', book.stop_slip)
        book.mark(book.usdt, open_ms, adverse=True)
        return True
    book.mark(book.equity_usdt(low), open_ms, adverse=True)
    return False


def simulate(bars, fx, *, start_ms: int, end_ms: int, sma_window: int, trail: str,
             fee=FEE, entry_slip=ENTRY_SLIP, exit_slip=EXIT_SLIP, stop_slip=STOP_SLIP,
             conversion=CONVERSION, skip_entries: set[int] | None = None):
    """Walk daily bars. ``bars`` begin at the model origin and are contiguous."""
    model = Model(sma_window, trail)
    book = Book(fx, start_ms, fee=fee, entry_slip=entry_slip, exit_slip=exit_slip,
                stop_slip=stop_slip, conversion=conversion)
    book.mark(book.usdt, start_ms, adverse=True)
    skip_entries = skip_entries or set()
    daily = []
    skipped = 0
    for open_ms, open_, high, low, close, _quote in bars:
        bull_prev = model.bull
        if open_ms >= end_ms:
            break
        in_window = open_ms >= start_ms
        exited = False
        if in_window and book.btc > 0 and not bull_prev:
            book.sell(open_, open_ms, 'sma', book.exit_slip)
            book.mark(book.usdt, open_ms, adverse=True)
            exited = True
        elif in_window and book.btc > 0:
            exited = _trail(book, model, open_ms, open_, high, low)
        if in_window and book.btc == 0 and not exited:
            book.mark(book.usdt, open_ms, adverse=True)
            if bull_prev and open_ms not in skip_entries:
                book.buy(open_, open_ms)
                book.peak_high = open_
                exited = _trail(book, model, open_ms, open_, high, low)
            elif bull_prev and open_ms in skip_entries:
                skipped += 1
        model.update(open_ms, high, low, close)
        if in_window:
            price = close if book.btc > 0 else D(0)
            equity = book.usdt if book.btc == 0 else book.equity_usdt(price)
            daily.append((open_ms, book._cny(equity, open_ms + DAY - 1)))
    last = [bar for bar in bars if bar[0] < end_ms][-1]
    final_usdt = book.usdt if book.btc == 0 else book.equity_usdt(last[4])
    final_cny = book._cny(final_usdt, end_ms - 1)
    years = D(end_ms - start_ms) / YEAR_MS
    growth = final_cny / INITIAL_CNY
    cagr = (float(growth) ** (1 / float(years)) - 1) if growth > 0 else -1
    return {
        'final_usdt': final_usdt,
        'final_cny': final_cny,
        'cagr': cagr,
        'mdd': book.mdd,
        'mdd_at': book.mdd_at,
        'fees': book.fees,
        'trades': book.trades,
        'position_btc': book.btc,
        'skipped_entries': skipped,
        'daily_cny': daily,
        'sma_window': sma_window,
        'trail': str(trail),
        'years': years,
    }
