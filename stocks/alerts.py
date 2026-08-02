"""Price alerts: simple rules saved locally and checked on demand.

Rules live in ``stocks_alerts.json`` (gitignored). Kinds:

- ``above VALUE``  — last price at or above VALUE (take-profit / breakout)
- ``below VALUE``  — last price at or below VALUE (buy-the-dip target)
- ``drop VALUE``   — price is VALUE% or more below its 52-week high
- ``day VALUE``    — today's move is VALUE% or larger in either direction

Run ``python -m stocks alerts`` to evaluate all rules against fresh quotes.
"""

import json
import os

ALERTS_FILE = "stocks_alerts.json"
KINDS = ("above", "below", "drop", "day")


class AlertError(Exception):
    pass


def load_alerts(path=ALERTS_FILE):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(rules, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2)


def add_alert(symbol, kind, value, path=ALERTS_FILE):
    if kind not in KINDS:
        raise AlertError("unknown alert kind %r (use one of: %s)"
                         % (kind, ", ".join(KINDS)))
    value = float(value)
    if kind in ("drop", "day") and value <= 0:
        raise AlertError("%s alerts need a positive percentage" % kind)
    rules = load_alerts(path)
    rule = {
        "id": max((r["id"] for r in rules), default=0) + 1,
        "symbol": symbol.upper(),
        "kind": kind,
        "value": value,
    }
    rules.append(rule)
    _save(rules, path)
    return rule


def remove_alert(target, path=ALERTS_FILE):
    """Remove by numeric id, or every rule for a symbol. Returns count."""
    rules = load_alerts(path)
    if str(target).isdigit():
        keep = [r for r in rules if r["id"] != int(target)]
    else:
        keep = [r for r in rules if r["symbol"] != str(target).upper()]
    removed = len(rules) - len(keep)
    if removed:
        _save(keep, path)
    return removed


def describe(rule):
    if rule["kind"] == "above":
        return "%s at or above %.2f" % (rule["symbol"], rule["value"])
    if rule["kind"] == "below":
        return "%s at or below %.2f" % (rule["symbol"], rule["value"])
    if rule["kind"] == "drop":
        return "%s down %.0f%%+ from 52-week high" % (rule["symbol"], rule["value"])
    return "%s daily move of %.0f%%+" % (rule["symbol"], rule["value"])


def check_alert(rule, quote):
    """Evaluate one rule against a Quote. Returns (triggered, detail)."""
    price = quote.last
    if rule["kind"] == "above":
        return price >= rule["value"], "last %.2f" % price
    if rule["kind"] == "below":
        return price <= rule["value"], "last %.2f" % price
    if rule["kind"] == "drop":
        off = quote.pct_off_high
        if off is None:
            return False, "no 52-week data"
        return off <= -rule["value"], "%.1f%% off high" % off
    day = quote.day_change_pct
    if day is None:
        return False, "no daily change"
    return abs(day) >= rule["value"], "day %+.1f%%" % day


def check_alerts(rules, quotes):
    """Evaluate rules against {symbol: Quote}. Returns a list of
    (rule, triggered, detail); rules whose quote is missing get
    triggered=None."""
    results = []
    for rule in rules:
        quote = quotes.get(rule["symbol"])
        if quote is None:
            results.append((rule, None, "no quote"))
            continue
        triggered, detail = check_alert(rule, quote)
        results.append((rule, triggered, detail))
    return results
