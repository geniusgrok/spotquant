"""One continuous spot account. The decision is spotquant.model.Model.

``simulate`` is one sleeve on the whole balance (the P3 book). ``simulate_sleeves``
runs several sleeves on one USDT pool (the P4 book) and prints the same trades and
final CNY as ``simulate`` when it has one sleeve. The description below is one sleeve.

Long or cash, no borrow, no short, no futures. An armed entry, including a
crash-reversal entry, buys at the next daily open. While long, a 28% stop is
modeled on the daily range by taking the high before the low. The meter's
peak starts at the fill open. An SMA exit, a blow-off exit, or a close 4% or
more under the entry fill sells at the next open. When those exits share an
open, the recorded kind is ``adverse``, then ``extend``, then ``sma``. A
crash-reversal hold ignores all three until the model hands the trade back.
Costs and the CNY conversion are applied here. Open, high, low, and flat
cash marks inside a bar use that bar's 00:00 UTC timestamp. The stored daily
curve and the final mark use the last millisecond of the UTC day, and those
CNY values also update the drawdown. A 17:00 DEXCHUS print therefore applies
to the close curve and to the drawdown on its date. The stop tested on a bar
is the stop from the prior completed peak. That bar's high tightens the stop
only after the low is tested, for the next day. If the tightened stop is
already through the close, the sell is the next open. Fills do not depend
on the rate.
"""
from __future__ import annotations

from decimal import Decimal as D
import math

from spotquant.model import (
    ADVERSE, CONFIRM, CRASH, DAY, FRESH, HIGH_WINDOW, Model, SLEEVES, SMA_WINDOW, TRAIL,
)

CONVERSION = D('0.001')
FEE = D('0.001')
ENTRY_SLIP = D('0.0005')
EXIT_SLIP = D('0.0005')
STOP_SLIP = D('0.001')
INITIAL_CNY = D('10000')
YEAR_MS = D('31556952000')  # 365.2425 * 86400 * 1000


class Book:
    def __init__(self, fx, start_ms: int, *, fee=FEE, entry_slip=ENTRY_SLIP, exit_slip=EXIT_SLIP,
                 stop_slip=STOP_SLIP, conversion=CONVERSION, initial_cny=INITIAL_CNY):
        self.fx = fx
        self.fee = D(fee)
        self.entry_slip = D(entry_slip)
        self.exit_slip = D(exit_slip)
        self.stop_slip = D(stop_slip)
        self.conversion = D(conversion)
        self.usdt = (D(initial_cny) / fx(start_ms)) * (D(1) - self.conversion)
        self.btc = D(0)
        self.fees = D(0)
        self.peak_cny = self._cny(self.usdt, start_ms)
        self.mdd = D(0)
        self.mdd_at = start_ms
        self.peak_high: D | None = None
        self.trades: list[dict] = []
        self._entry_px = D(0)
        self._entry_ms = 0
        self.exit_next: str | None = None

    def _cny(self, usdt_equity: D, now_ms: int) -> D:
        return usdt_equity * self.fx(now_ms) * (D(1) - self.conversion)

    def mark(self, usdt_equity: D, now_ms: int, *, adverse: bool):
        self.mark_cny(self._cny(usdt_equity, now_ms), now_ms, adverse=adverse)

    def mark_cny(self, cny: D, now_ms: int, *, adverse: bool):
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
        self.exit_next = None


def _trail(book: Book, model: Model, open_ms: int, open_: D, high: D, low: D, close: D) -> bool:
    """Resting stop from the prior peak. True when it sells on this bar.

    A gap through that stop sells at the open. The low is tested against the
    same stop. The high tightens the stop only afterward. A tightened stop
    that is already through the close sells at the next open.
    """
    stop = model.stop_price(book.peak_high)
    if open_ <= stop:
        book.sell(open_, open_ms, 'gap', book.stop_slip)
        book.mark(book.usdt, open_ms, adverse=True)
        return True
    book.mark(book.equity_usdt(high), open_ms, adverse=False)
    if low <= stop:
        book.sell(stop, open_ms, 'trail', book.stop_slip)
        book.mark(book.usdt, open_ms, adverse=True)
        return True
    book.mark(book.equity_usdt(low), open_ms, adverse=True)
    if high > book.peak_high:
        book.peak_high = high
    if close <= model.stop_price(book.peak_high):
        book.exit_next = 'stop'
    return False


def simulate(bars, fx, *, start_ms: int, end_ms: int, sma_window: int | None = None,
             trail: str | None = None, confirm: int | None = None, crash: str | None = None,
             fresh: bool | None = None, high_window: int | None = None,
             fee=FEE, entry_slip=ENTRY_SLIP, exit_slip=EXIT_SLIP, stop_slip=STOP_SLIP,
             conversion=CONVERSION, skip_entries: set[int] | None = None,
             adverse_stop: str | None = None):
    """Walk daily bars. ``bars`` begin at the model origin and are contiguous."""
    model = Model(
        SMA_WINDOW if sma_window is None else sma_window,
        TRAIL if trail is None else trail,
        CONFIRM if confirm is None else confirm,
        CRASH if crash is None else crash,
        HIGH_WINDOW if high_window is None else high_window,
        FRESH if fresh is None else fresh,
        adverse_stop=ADVERSE if adverse_stop is None else adverse_stop,
    )
    book = Book(fx, start_ms, fee=fee, entry_slip=entry_slip, exit_slip=exit_slip,
                stop_slip=stop_slip, conversion=conversion)
    book.mark(book.usdt, start_ms, adverse=True)
    skip_entries = skip_entries or set()
    daily = []
    skipped = 0
    for open_ms, open_, high, low, close, _quote in bars:
        bull_prev = model.bull
        enter_prev = model.enter
        extend_prev = model.extended
        cap_prev = model.cap_enter
        repair_prev = model.repair
        adverse_prev = model.adverse
        if open_ms >= end_ms:
            break
        in_window = open_ms >= start_ms
        exited = False
        if in_window and book.btc > 0 and book.exit_next:
            book.sell(open_, open_ms, book.exit_next, book.exit_slip)
            book.mark(book.usdt, open_ms, adverse=True)
            model.note_exit()
            exited = True
        # Repair keeps only the 28% stop. Otherwise adverse wins over blow-off and SMA
        # when they fall on the same open, because the fill is the same.
        elif in_window and book.btc > 0 and not repair_prev and (adverse_prev or extend_prev or not bull_prev):
            if adverse_prev:
                kind = 'adverse'
            elif extend_prev:
                kind = 'extend'
            else:
                kind = 'sma'
            book.sell(open_, open_ms, kind, book.exit_slip)
            book.mark(book.usdt, open_ms, adverse=True)
            model.note_exit()
            exited = True
        elif in_window and book.btc > 0:
            exited = _trail(book, model, open_ms, open_, high, low, close)
            if exited:
                model.note_exit()
        if in_window and book.btc == 0 and not exited:
            book.mark(book.usdt, open_ms, adverse=True)
            armed = enter_prev or cap_prev
            if armed and open_ms not in skip_entries:
                book.buy(open_, open_ms)
                book.peak_high = open_
                model.note_entry(book._entry_px)
                if cap_prev:
                    model.note_cap_entry()
                exited = _trail(book, model, open_ms, open_, high, low, close)
                if exited:
                    model.note_exit()
            elif armed and open_ms in skip_entries:
                skipped += 1
        model.update(open_ms, high, low, close)
        if in_window:
            price = close if book.btc > 0 else D(0)
            equity = book.usdt if book.btc == 0 else book.equity_usdt(price)
            close_cny = book._cny(equity, open_ms + DAY - 1)
            book.mark_cny(close_cny, open_ms + DAY - 1, adverse=True)
            daily.append((open_ms, close_cny))
    last = [bar for bar in bars if bar[0] < end_ms][-1]
    final_usdt = book.usdt if book.btc == 0 else book.equity_usdt(last[4])
    final_cny = book._cny(final_usdt, end_ms - 1)
    book.mark_cny(final_cny, end_ms - 1, adverse=True)
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
        'sma_window': model.sma_window,
        'trail': format(model.trail, 'f'),
        'confirm': model.confirm,
        'crash': format(model.crash, 'f'),
        'fresh': model.fresh,
        'extend': format(model.extend, 'f'),
        'cap_drop': format(model.cap_drop, 'f'),
        'cap_bounce': format(model.cap_bounce, 'f'),
        'cap_depth': format(model.cap_depth, 'f'),
        'cap_hand': format(model.cap_hand, 'f'),
        'cap_window': model.cap_window,
        'adverse_stop': format(model.adverse_stop, 'f'),
        'years': years,
    }


VOL_TARGET = D('0.70')
VOL_WINDOW = 30


def realized_vol(closes) -> D | None:
    """Annualized sample deviation of the last VOL_WINDOW daily log returns."""
    values = list(closes)[-(VOL_WINDOW + 1):]
    if len(values) < VOL_WINDOW + 1:
        return None
    returns = [math.log(float(b) / float(a)) for a, b in zip(values, values[1:])]
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return D(repr(math.sqrt(variance * 365)))


class _Meter:
    """Continuous mark-to-market drawdown of the combined CNY equity."""

    def __init__(self, fx, conversion):
        self.fx = fx
        self.conversion = conversion
        self.peak = D(0)
        self.mdd = D(0)
        self.mdd_at = 0

    def cny(self, usdt_equity: D, now_ms: int) -> D:
        return usdt_equity * self.fx(now_ms) * (D(1) - self.conversion)

    def mark(self, usdt_equity: D, now_ms: int, *, adverse: bool):
        self.mark_cny(self.cny(usdt_equity, now_ms), now_ms, adverse=adverse)

    def mark_cny(self, value: D, now_ms: int, *, adverse: bool):
        if value > self.peak:
            self.peak = value
        if adverse and self.peak > 0:
            drawdown = D(1) - value / self.peak
            if drawdown > self.mdd:
                self.mdd = drawdown
                self.mdd_at = now_ms


class _Sleeve:
    def __init__(self, window: int, model: Model):
        self.window = window
        self.model = model
        self.btc = D(0)
        self.peak_high = D(0)
        self.entry_px = D(0)
        self.entry_ms = 0
        self.exit_next: str | None = None


def simulate_sleeves(bars, fx, *, start_ms: int, end_ms: int, windows=SLEEVES, model_kwargs=None,
                     fee=FEE, entry_slip=ENTRY_SLIP, exit_slip=EXIT_SLIP, stop_slip=STOP_SLIP,
                     conversion=CONVERSION, skip_entries: set[int] | None = None,
                     vol_target=None, cold_start: bool = False, initial_cny=INITIAL_CNY,
                     frozen: set[int] | None = None):
    """Several SMA sleeves on one USDT pool. Each sleeve is a ``Model`` with its own window.

    An armed sleeve buys at the open with the pool divided by the number of sleeves
    that hold no coins after that open's exits. Each sleeve exits and stops on its own
    coins. With one sleeve this prints the trades and the final CNY of ``simulate``.
    """
    fee, entry_slip, exit_slip = D(fee), D(entry_slip), D(exit_slip)
    stop_slip, conversion = D(stop_slip), D(conversion)
    extra = dict(model_kwargs or {})
    sleeves = [_Sleeve(window, Model(window, **extra)) for window in windows]
    meter = _Meter(fx, conversion)
    pool = (D(initial_cny) / fx(start_ms)) * (D(1) - conversion)
    meter.mark(pool, start_ms, adverse=True)
    skip_entries = skip_entries or set()
    frozen = frozen or set()
    trades: list[dict] = []
    fees = D(0)
    skipped = 0
    daily = []

    def equity(price):
        return pool + sum((s.btc * price for s in sleeves), D(0))

    def sell(sleeve, price, now_ms, kind, slip):
        nonlocal pool, fees
        fill = price * (D(1) - slip)
        gross = sleeve.btc * fill
        charge = gross * fee
        fees += charge
        pool += gross - charge
        trades.append({
            'sleeve': sleeve.window,
            'entry_ms': sleeve.entry_ms,
            'exit_ms': now_ms,
            'entry': format(sleeve.entry_px, 'f'),
            'exit': format(fill, 'f'),
            'kind': kind,
        })
        sleeve.btc = D(0)
        sleeve.exit_next = None

    started = False
    for open_ms, open_, high, low, close, _quote in bars:
        if open_ms >= end_ms:
            break
        in_window = open_ms >= start_ms
        prev = {s.window: (s.model.bull, s.model.enter, s.model.extended, s.model.cap_enter,
                           s.model.repair, s.model.adverse) for s in sleeves}
        closes_before = list(sleeves[0].model.closes)
        blocked_entries = set(skip_entries)
        if in_window and cold_start and not started:
            for sleeve in sleeves:
                sleeve.model.note_flat()
            prev = {s.window: (s.model.bull, s.model.enter, s.model.extended, s.model.cap_enter,
                               s.model.repair, s.model.adverse) for s in sleeves}
            blocked_entries.add(open_ms)
        if in_window:
            started = True
            quiet = open_ms in frozen
            exited = set()
            acted = False
            for sleeve in sleeves:
                if sleeve.btc > 0 and sleeve.exit_next and not quiet:
                    sell(sleeve, open_, open_ms, sleeve.exit_next, exit_slip)
                    sleeve.model.note_exit()
                    exited.add(sleeve.window)
                    acted = True
            for sleeve in sleeves:
                bull, _enter, extended, _cap, repair, adverse = prev[sleeve.window]
                if quiet or sleeve.window in exited:
                    continue
                if sleeve.btc > 0 and not repair and (adverse or extended or not bull):
                    kind = 'adverse' if adverse else ('extend' if extended else 'sma')
                    sell(sleeve, open_, open_ms, kind, exit_slip)
                    sleeve.model.note_exit()
                    exited.add(sleeve.window)
                    acted = True
            for sleeve in sleeves:
                if sleeve.btc > 0 and sleeve.window not in exited:
                    stop = sleeve.model.stop_price(sleeve.peak_high)
                    if open_ <= stop:
                        sell(sleeve, open_, open_ms, 'gap', stop_slip)
                        sleeve.model.note_exit()
                        exited.add(sleeve.window)
                        acted = True
            flat = [s for s in sleeves if s.btc == 0]
            if flat or acted:
                meter.mark(equity(open_), open_ms, adverse=True)
            budget = pool / len(flat) if flat else D(0)
            for sleeve in flat:
                _bull, enter, _extended, cap_enter, _repair, _adverse = prev[sleeve.window]
                if quiet or sleeve.window in exited or not (enter or cap_enter):
                    continue
                if open_ms in blocked_entries:
                    skipped += 1
                    continue
                spend = min(budget, pool)
                if vol_target is not None:
                    vol = realized_vol(closes_before)
                    if vol is not None and vol > D(vol_target):
                        spend = spend * D(vol_target) / vol
                charge = spend * fee
                fees += charge
                fill = open_ * (D(1) + entry_slip)
                sleeve.btc = (spend - charge) / fill
                pool -= spend
                sleeve.entry_px = fill
                sleeve.entry_ms = open_ms
                sleeve.peak_high = open_
                sleeve.model.note_entry(fill)
                if cap_enter:
                    sleeve.model.note_cap_entry()
            holders = [s for s in sleeves if s.btc > 0 and s.window not in exited]
            if holders:
                meter.mark(equity(high), open_ms, adverse=False)
                stopped = []
                for sleeve in holders:
                    stop = sleeve.model.stop_price(sleeve.peak_high)
                    if low <= stop:
                        stopped.append((sleeve, stop))
                for sleeve, stop in stopped:
                    sell(sleeve, stop, open_ms, 'trail', stop_slip)
                    sleeve.model.note_exit()
                    exited.add(sleeve.window)
                for sleeve in holders:
                    if sleeve.window in exited or quiet:
                        continue
                    if high > sleeve.peak_high:
                        sleeve.peak_high = high
                    if close <= sleeve.model.stop_price(sleeve.peak_high):
                        sleeve.exit_next = 'stop'
                meter.mark(equity(low), open_ms, adverse=True)
        for sleeve in sleeves:
            sleeve.model.update(open_ms, high, low, close)
        if in_window:
            close_cny = meter.cny(equity(close), open_ms + DAY - 1)
            meter.mark_cny(close_cny, open_ms + DAY - 1, adverse=True)
            daily.append((open_ms, close_cny))
    last = [bar for bar in bars if bar[0] < end_ms][-1]
    final_usdt = equity(last[4])
    final_cny = meter.cny(final_usdt, end_ms - 1)
    meter.mark_cny(final_cny, end_ms - 1, adverse=True)
    years = D(end_ms - start_ms) / YEAR_MS
    growth = final_cny / D(initial_cny)
    cagr = (float(growth) ** (1 / float(years)) - 1) if growth > 0 else -1
    return {
        'final_usdt': final_usdt,
        'final_cny': final_cny,
        'cagr': cagr,
        'mdd': meter.mdd,
        'mdd_at': meter.mdd_at,
        'fees': fees,
        'trades': sorted(trades, key=lambda item: (item['exit_ms'], item['sleeve'])),
        'position_btc': sum((s.btc for s in sleeves), D(0)),
        'positions': {s.window: s.btc for s in sleeves},
        'skipped_entries': skipped,
        'daily_cny': daily,
        'windows': list(windows),
        'years': years,
        'model': sleeves[0].model,
    }
