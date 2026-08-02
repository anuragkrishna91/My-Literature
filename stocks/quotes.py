"""Quote fetching and metric computation.

Primary source is Yahoo Finance's public chart endpoint (one request per
symbol returns a year of daily closes — enough for every metric we show).
If Yahoo fails, we fall back to Stooq's free CSV history. Neither needs an
API key. Requests are rate-limited and sent with a real User-Agent, in
keeping with this repo's "polite by default" rule.
"""

import csv
import io
import time
from datetime import date, datetime

import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ_HISTORY_URL = "https://stooq.com/q/d/l/"
REQUEST_DELAY_S = 0.4  # polite spacing between symbol fetches
TIMEOUT_S = 15


class QuoteError(Exception):
    pass


class Quote:
    """A symbol's daily close history plus derived metrics."""

    def __init__(self, symbol, dates, closes, currency="USD"):
        if len(dates) != len(closes) or not closes:
            raise QuoteError("empty or mismatched history for %s" % symbol)
        self.symbol = symbol
        self.dates = dates      # list[date], ascending
        self.closes = closes    # list[float], aligned with dates
        self.currency = currency

    @property
    def last(self):
        return self.closes[-1]

    def _pct_from(self, index):
        base = self.closes[index]
        if base == 0:
            return None
        return (self.last - base) / base * 100.0

    @property
    def day_change_pct(self):
        if len(self.closes) < 2:
            return None
        return self._pct_from(-2)

    @property
    def month_change_pct(self):
        """Change vs ~21 trading days ago (one calendar month)."""
        if len(self.closes) < 22:
            return None
        return self._pct_from(-22)

    @property
    def ytd_change_pct(self):
        """Change vs the last close of the previous calendar year. If the
        history doesn't reach back that far, measure from its start."""
        year = self.dates[-1].year
        base_index = 0
        for i, d in enumerate(self.dates):
            if d.year == year:
                base_index = max(0, i - 1)
                break
        return self._pct_from(base_index)

    @property
    def high_52w(self):
        return max(self.closes)

    @property
    def low_52w(self):
        return min(self.closes)

    @property
    def pct_off_high(self):
        """How far below the 52-week high the last close sits (<= 0)."""
        high = self.high_52w
        if high == 0:
            return None
        return (self.last - high) / high * 100.0

    @property
    def trend(self):
        """'up' when the 50-day average is above the 200-day, else 'down';
        None when there isn't enough history."""
        if len(self.closes) < 200:
            return None
        sma50 = sum(self.closes[-50:]) / 50.0
        sma200 = sum(self.closes[-200:]) / 200.0
        return "up" if sma50 > sma200 else "down"


def _parse_yahoo_chart(payload, symbol):
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise QuoteError("no chart data for %s" % symbol)
    node = result[0]
    timestamps = node.get("timestamp") or []
    quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    raw_closes = quote.get("close") or []
    currency = (node.get("meta") or {}).get("currency") or "USD"
    dates, closes = [], []
    for ts, close in zip(timestamps, raw_closes):
        if close is None:
            continue
        dates.append(datetime.utcfromtimestamp(ts).date())
        closes.append(float(close))
    return Quote(symbol, dates, closes, currency)


def _fetch_yahoo(session, symbol):
    resp = session.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "1y", "interval": "1d"},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise QuoteError("Yahoo returned HTTP %d for %s" % (resp.status_code, symbol))
    return _parse_yahoo_chart(resp.json(), symbol)


def _stooq_symbol(symbol):
    """Map a US ticker to Stooq's naming (lowercase + '.us'); FX pairs like
    EURUSD=X become plain 'eurusd'."""
    if symbol.endswith("=X"):
        return symbol[:-2].lower()
    return symbol.lower() + ".us"


def _parse_stooq_csv(text, symbol):
    reader = csv.DictReader(io.StringIO(text))
    dates, closes = [], []
    for row in reader:
        try:
            d = datetime.strptime(row["Date"], "%Y-%m-%d").date()
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        dates.append(d)
        closes.append(close)
    if not closes:
        raise QuoteError("no Stooq data for %s" % symbol)
    # Stooq returns full history; keep roughly the last trading year.
    return Quote(symbol, dates[-260:], closes[-260:])


def _fetch_stooq(session, symbol):
    resp = session.get(
        STOOQ_HISTORY_URL,
        params={"s": _stooq_symbol(symbol), "i": "d"},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise QuoteError("Stooq returned HTTP %d for %s" % (resp.status_code, symbol))
    return _parse_stooq_csv(resp.text, symbol)


def fetch_quote(symbol, session=None):
    """Fetch one symbol's history, trying Yahoo then Stooq."""
    session = session or requests.Session()
    errors = []
    for fetcher in (_fetch_yahoo, _fetch_stooq):
        try:
            return fetcher(session, symbol)
        except (QuoteError, requests.RequestException, ValueError) as exc:
            errors.append(str(exc))
    raise QuoteError("; ".join(errors))


def fetch_quotes(symbols, session=None, delay_s=REQUEST_DELAY_S, on_error=None):
    """Fetch many symbols politely. Returns {symbol: Quote}; failures are
    reported through on_error(symbol, message) and skipped."""
    session = session or requests.Session()
    quotes = {}
    for i, symbol in enumerate(symbols):
        if i:
            time.sleep(delay_s)
        try:
            quotes[symbol] = fetch_quote(symbol, session)
        except QuoteError as exc:
            if on_error:
                on_error(symbol, str(exc))
    return quotes


def fetch_eur_usd(session=None):
    """EURUSD rate for showing portfolio totals in euros; None on failure."""
    try:
        return fetch_quote("EURUSD=X", session).last
    except QuoteError:
        return None
