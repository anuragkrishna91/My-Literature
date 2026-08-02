"""Offline tests for the stock tracker (no network required).

We stub the HTTP session so quote fetching, metric math, watchlist
management, and portfolio valuation can be verified without reaching
Yahoo or Stooq.
"""

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocks.alerts import (add_alert, check_alerts, describe, load_alerts,
                           remove_alert)
from stocks.analysis import recommend, rsi
from stocks.portfolio import Holding, load_portfolio, value_portfolio
from stocks.quotes import (Quote, _parse_stooq_csv, _parse_yahoo_chart,
                           _stooq_symbol, fetch_quote)
from stocks.watchlist import (add_symbol, all_symbols, load_watchlist,
                              remove_symbol)


class FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    """Routes URLs to canned responses by substring match."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return FakeResponse(status=404)


def _quote_from_closes(closes, end=date(2026, 8, 1)):
    dates = [end - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))]
    return Quote("TEST", dates, closes)


def test_quote_metrics():
    q = _quote_from_closes([100.0, 110.0, 121.0])
    assert q.last == 121.0
    assert abs(q.day_change_pct - 10.0) < 1e-9
    assert q.high_52w == 121.0 and q.low_52w == 100.0
    assert abs(q.pct_off_high) < 1e-9
    assert q.month_change_pct is None  # too little history
    assert q.trend is None


def test_ytd_uses_last_close_of_previous_year():
    # Dec 30, Dec 31, then two January closes.
    dates = [date(2025, 12, 30), date(2025, 12, 31),
             date(2026, 1, 2), date(2026, 1, 5)]
    q = Quote("TEST", dates, [90.0, 100.0, 105.0, 120.0])
    assert abs(q.ytd_change_pct - 20.0) < 1e-9


def test_trend_up_and_down():
    # 200 flat days then a strong recent run -> 50-day SMA above 200-day.
    up = _quote_from_closes([100.0] * 200 + [200.0] * 60)
    assert up.trend == "up"
    down = _quote_from_closes([200.0] * 200 + [100.0] * 60)
    assert down.trend == "down"


def test_parse_yahoo_chart_skips_null_closes():
    payload = {"chart": {"result": [{
        "meta": {"currency": "USD"},
        "timestamp": [1700000000, 1700086400, 1700172800],
        "indicators": {"quote": [{"close": [10.0, None, 12.0]}]},
    }]}}
    q = _parse_yahoo_chart(payload, "MSFT")
    assert q.closes == [10.0, 12.0]
    assert q.currency == "USD"


def test_stooq_fallback_when_yahoo_fails():
    stooq_csv = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        "2026-07-%02d,1,1,1,%d,100" % (i, 100 + i) for i in range(1, 11))
    session = FakeSession({
        "query1.finance.yahoo.com": FakeResponse(status=500),
        "stooq.com": FakeResponse(text=stooq_csv),
    })
    q = fetch_quote("MSFT", session)
    assert q.last == 110.0
    assert any("stooq.com" in url for url in session.calls)


def test_stooq_symbol_mapping():
    assert _stooq_symbol("MSFT") == "msft.us"
    assert _stooq_symbol("EURUSD=X") == "eurusd"


def test_parse_stooq_keeps_last_year_only():
    rows = ["Date,Open,High,Low,Close,Volume"]
    d = date(2024, 1, 1)
    for i in range(400):
        rows.append("%s,1,1,1,%d,100" % (d + timedelta(days=i), i))
    q = _parse_stooq_csv("\n".join(rows), "MSFT")
    assert len(q.closes) == 260
    assert q.last == 399.0


def test_watchlist_add_remove(tmp_path):
    path = str(tmp_path / "watchlist.json")
    add_symbol("shop", "Software", name="Shopify", path=path)
    watchlist = load_watchlist(path)
    assert ("SHOP", "Shopify") in watchlist["Software"]
    # Defaults were materialized into the file too.
    assert any(sym == "MSFT" for sym, _ in watchlist["Software"])
    assert remove_symbol("SHOP", path=path)
    assert not remove_symbol("SHOP", path=path)
    flat = all_symbols(load_watchlist(path))
    assert all(sym != "SHOP" for _, sym, _ in flat)


def test_portfolio_valuation(tmp_path):
    csv_path = tmp_path / "portfolio.csv"
    csv_path.write_text(
        "# my degiro holdings\n"
        "symbol,shares,cost_per_share_usd\n"
        "MSFT,2,400\n"
        "NVDA,4,100\n"
    )
    holdings = load_portfolio(str(csv_path))
    rows, totals = value_portfolio(holdings, {"MSFT": 500.0})  # NVDA quote failed
    msft = rows[0]
    assert msft["value"] == 1000.0 and msft["gain"] == 200.0
    assert abs(msft["gain_pct"] - 25.0) < 1e-9
    assert rows[1]["price"] is None and rows[1]["value"] is None
    # Totals only include the priced holding.
    assert totals["cost_basis"] == 800.0 and totals["value"] == 1000.0


def test_rsi_bounds_and_direction():
    assert rsi([100.0] * 5) is None  # too short
    only_up = [100.0 + i for i in range(30)]
    assert rsi(only_up) == 100.0
    only_down = [100.0 - i for i in range(30)]
    assert rsi(only_down) == 0.0
    # Equal-sized alternating gains and losses hover around 50.
    wobble = [100.0 + (1 if i % 2 else 0) for i in range(40)]
    assert 40.0 < rsi(wobble) < 60.0


def test_recommend_scores_uptrend_higher():
    strong = _quote_from_closes(
        [100.0] * 200 + [100.0 + i for i in range(60)])
    weak = _quote_from_closes(
        [200.0] * 200 + [200.0 - i for i in range(60)])
    rec_strong = recommend(strong)
    rec_weak = recommend(weak)
    assert rec_strong["score"] > rec_weak["score"]
    assert rec_weak["label"] == "Weak"
    assert any("uptrend" in reason for reason in rec_strong["reasons"])
    assert rec_strong["reasons"]  # every point of score is explained


def test_alert_lifecycle(tmp_path):
    path = str(tmp_path / "alerts.json")
    rule1 = add_alert("nvda", "below", 150, path=path)
    rule2 = add_alert("MSFT", "drop", 15, path=path)
    assert rule1["symbol"] == "NVDA" and rule1["id"] == 1
    assert "below 150.00" in describe(rule1)
    assert len(load_alerts(path)) == 2
    assert remove_alert(str(rule2["id"]), path=path) == 1
    add_alert("NVDA", "day", 5, path=path)
    assert remove_alert("nvda", path=path) == 2  # removes both NVDA rules
    assert load_alerts(path) == []


def test_alert_triggering():
    quote = _quote_from_closes([200.0] * 5 + [100.0])  # crashed 50% today
    rules = [
        {"id": 1, "symbol": "TEST", "kind": "below", "value": 150.0},
        {"id": 2, "symbol": "TEST", "kind": "above", "value": 150.0},
        {"id": 3, "symbol": "TEST", "kind": "drop", "value": 20.0},
        {"id": 4, "symbol": "TEST", "kind": "day", "value": 10.0},
        {"id": 5, "symbol": "MISSING", "kind": "below", "value": 1.0},
    ]
    results = check_alerts(rules, {"TEST": quote})
    by_id = {rule["id"]: triggered for rule, triggered, _ in results}
    assert by_id[1] is True    # below 150
    assert by_id[2] is False   # not above 150
    assert by_id[3] is True    # 50% off high
    assert by_id[4] is True    # 50% daily move
    assert by_id[5] is None    # no quote available


def test_holding_math():
    h = Holding("msft", 3, 100.0)
    assert h.symbol == "MSFT"
    assert h.cost_basis == 300.0
    assert h.gain(120.0) == 60.0
    assert abs(h.gain_pct(120.0) - 20.0) < 1e-9
