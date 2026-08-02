"""Command-line interface: watch, portfolio, add, remove, list."""

import argparse
import json
import sys

from .alerts import (ALERTS_FILE, AlertError, add_alert, check_alerts,
                     describe, load_alerts, remove_alert)
from .analysis import recommend, rsi
from .portfolio import PortfolioError, load_portfolio, value_portfolio
from .quotes import fetch_eur_usd, fetch_quotes
from .watchlist import (WATCHLIST_FILE, add_symbol, all_symbols,
                        load_watchlist, remove_symbol)

NOT_ADVICE = ("Signals are mechanical (trend/momentum/RSI), informational "
              "only — not financial advice.")


def _fmt(value, pattern="%.2f", empty="-"):
    return empty if value is None else pattern % value


def _fmt_pct(value):
    return "-" if value is None else "%+.1f%%" % value


def _print_table(headers, rows, indent=""):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def line(cells):
        # first column left-aligned, numbers right-aligned
        parts = [cells[0].ljust(widths[0])]
        parts += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        print(indent + "  ".join(parts))
    line(headers)
    line(["-" * w for w in widths])
    for row in rows:
        line(row)


def cmd_watch(args):
    watchlist = load_watchlist()
    flat = all_symbols(watchlist)
    symbols = [symbol for _, symbol, _ in flat]
    failures = []
    print("Fetching %d quotes..." % len(symbols), file=sys.stderr)
    quotes = fetch_quotes(symbols, on_error=lambda s, m: failures.append((s, m)))

    if args.json:
        out = {}
        for sector, symbol, name in flat:
            q = quotes.get(symbol)
            if not q:
                continue
            out.setdefault(sector, []).append({
                "symbol": symbol, "name": name, "price": q.last,
                "day_pct": q.day_change_pct, "month_pct": q.month_change_pct,
                "ytd_pct": q.ytd_change_pct, "high_52w": q.high_52w,
                "low_52w": q.low_52w, "pct_off_high": q.pct_off_high,
                "trend": q.trend, "rsi": rsi(q.closes),
            })
        print(json.dumps(out, indent=2))
    else:
        headers = ["Company", "Sym", "Price", "Day", "1M", "YTD",
                   "52w Low", "52w High", "Off High", "RSI", "Trend"]
        for sector in watchlist:
            entries = [(s, n) for sec, s, n in flat if sec == sector]
            sector_rows = []
            day_moves = []
            for symbol, name in entries:
                q = quotes.get(symbol)
                if not q:
                    sector_rows.append([name, symbol] + ["-"] * 9)
                    continue
                if q.day_change_pct is not None:
                    day_moves.append(q.day_change_pct)
                sector_rows.append([
                    name, symbol, _fmt(q.last), _fmt_pct(q.day_change_pct),
                    _fmt_pct(q.month_change_pct), _fmt_pct(q.ytd_change_pct),
                    _fmt(q.low_52w), _fmt(q.high_52w),
                    _fmt_pct(q.pct_off_high), _fmt(rsi(q.closes), "%.0f"),
                    q.trend or "-",
                ])
            avg = sum(day_moves) / len(day_moves) if day_moves else None
            print()
            print("%s  (avg day move %s)" % (sector, _fmt_pct(avg)))
            _print_table(headers, sector_rows, indent="  ")

    for symbol, message in failures:
        print("warning: %s: %s" % (symbol, message), file=sys.stderr)
    return 0


def cmd_portfolio(args):
    try:
        holdings = load_portfolio(args.file)
    except PortfolioError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    symbols = sorted({h.symbol for h in holdings})
    print("Fetching %d quotes..." % len(symbols), file=sys.stderr)
    failures = []
    quotes = fetch_quotes(symbols, on_error=lambda s, m: failures.append((s, m)))
    prices = {symbol: q.last for symbol, q in quotes.items()}
    rows, totals = value_portfolio(holdings, prices)

    headers = ["Sym", "Shares", "Avg Cost", "Price", "Cost Basis",
               "Value", "Gain $", "Gain %"]
    table = [[
        r["symbol"], _fmt(r["shares"], "%g"), _fmt(r["cost_per_share"]),
        _fmt(r["price"]), _fmt(r["cost_basis"]), _fmt(r["value"]),
        _fmt(r["gain"], "%+.2f"), _fmt_pct(r["gain_pct"]),
    ] for r in rows]
    print()
    _print_table(headers, table)
    print()
    print("Total (USD): cost %s -> value %s, gain %s (%s)" % (
        _fmt(totals["cost_basis"]), _fmt(totals["value"]),
        _fmt(totals["gain"], "%+.2f"), _fmt_pct(totals["gain_pct"])))

    eur_usd = fetch_eur_usd()
    if eur_usd:
        print("Total (EUR at %.4f): value %.2f, gain %+.2f" % (
            eur_usd, totals["value"] / eur_usd, totals["gain"] / eur_usd))

    for symbol, message in failures:
        print("warning: %s: %s" % (symbol, message), file=sys.stderr)
    return 0


def cmd_add(args):
    add_symbol(args.symbol, args.sector, args.name)
    print("Added %s to '%s' (saved to %s)" % (
        args.symbol.upper(), args.sector, WATCHLIST_FILE))
    return 0


def cmd_remove(args):
    if remove_symbol(args.symbol):
        print("Removed %s (saved to %s)" % (args.symbol.upper(), WATCHLIST_FILE))
        return 0
    print("%s is not on the watchlist" % args.symbol.upper(), file=sys.stderr)
    return 1


def cmd_recommend(args):
    watchlist = load_watchlist()
    flat = all_symbols(watchlist)
    names = {symbol: name for _, symbol, name in flat}
    symbols = [symbol for _, symbol, _ in flat]
    failures = []
    print("Fetching %d quotes..." % len(symbols), file=sys.stderr)
    quotes = fetch_quotes(symbols, on_error=lambda s, m: failures.append((s, m)))

    scored = []
    for symbol, quote in quotes.items():
        rec = recommend(quote)
        rec["symbol"] = symbol
        rec["name"] = names.get(symbol, symbol)
        scored.append(rec)
    scored.sort(key=lambda r: r["score"], reverse=True)

    if args.json:
        print(json.dumps(scored, indent=2))
    else:
        print()
        for rec in scored:
            print("%-8s %-22s score %+d  %-8s RSI %s" % (
                rec["symbol"], rec["name"], rec["score"], rec["label"],
                _fmt(rec["rsi"], "%.0f")))
            for reason in rec["reasons"]:
                print("           - %s" % reason)
        print()
        print(NOT_ADVICE)

    for symbol, message in failures:
        print("warning: %s: %s" % (symbol, message), file=sys.stderr)
    return 0


def cmd_alert_add(args):
    try:
        rule = add_alert(args.symbol, args.kind, args.value)
    except (AlertError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("Alert #%d added: %s (saved to %s)" % (
        rule["id"], describe(rule), ALERTS_FILE))
    return 0


def cmd_alert_remove(args):
    removed = remove_alert(args.target)
    if removed:
        print("Removed %d alert(s)" % removed)
        return 0
    print("no alert matches %r" % args.target, file=sys.stderr)
    return 1


def cmd_alert_list(args):
    rules = load_alerts()
    if not rules:
        print("No alerts set. Add one with: "
              "python -m stocks alert add NVDA below 150")
        return 0
    for rule in rules:
        print("#%-3d %s" % (rule["id"], describe(rule)))
    return 0


def cmd_alerts_check(args):
    rules = load_alerts()
    if not rules:
        print("No alerts set. Add one with: "
              "python -m stocks alert add NVDA below 150")
        return 0
    symbols = sorted({rule["symbol"] for rule in rules})
    failures = []
    print("Fetching %d quotes..." % len(symbols), file=sys.stderr)
    quotes = fetch_quotes(symbols, on_error=lambda s, m: failures.append((s, m)))

    results = check_alerts(rules, quotes)
    fired = [(r, d) for r, t, d in results if t]
    quiet = [(r, d) for r, t, d in results if t is False]
    unknown = [(r, d) for r, t, d in results if t is None]

    if fired:
        print()
        print("TRIGGERED:")
        for rule, detail in fired:
            print("  ! #%d %s  (%s)" % (rule["id"], describe(rule), detail))
    else:
        print()
        print("No alerts triggered.")
    if quiet and args.verbose:
        print("Not triggered:")
        for rule, detail in quiet:
            print("    #%d %s  (%s)" % (rule["id"], describe(rule), detail))
    for rule, detail in unknown:
        print("warning: #%d %s: %s" % (rule["id"], describe(rule), detail),
              file=sys.stderr)
    for symbol, message in failures:
        print("warning: %s: %s" % (symbol, message), file=sys.stderr)
    return 0 if not fired else 2


def cmd_list(args):
    for sector, entries in load_watchlist().items():
        print("%s:" % sector)
        for symbol, name in entries:
            print("  %-6s %s" % (symbol, name))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m stocks",
        description="Track US software/AI/biotech/medicine stocks and value "
                    "your Degiro holdings.",
    )
    sub = parser.add_subparsers(dest="command")

    p_watch = sub.add_parser("watch", help="show the sector watchlist (default)")
    p_watch.add_argument("--json", action="store_true", help="machine-readable output")
    p_watch.set_defaults(func=cmd_watch, json=False)

    p_pf = sub.add_parser("portfolio", help="value your holdings from a CSV")
    p_pf.add_argument("--file", default="portfolio.csv",
                      help="holdings CSV (default: portfolio.csv)")
    p_pf.set_defaults(func=cmd_portfolio)

    p_add = sub.add_parser("add", help="add a ticker to the watchlist")
    p_add.add_argument("symbol")
    p_add.add_argument("--sector", required=True, help='e.g. "Biotech"')
    p_add.add_argument("--name", help="company name (defaults to the symbol)")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove", help="remove a ticker from the watchlist")
    p_rm.add_argument("symbol")
    p_rm.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="show the watchlist without fetching")
    p_list.set_defaults(func=cmd_list)

    p_rec = sub.add_parser(
        "recommend",
        help="rank the watchlist by mechanical trend/momentum/RSI signals")
    p_rec.add_argument("--json", action="store_true", help="machine-readable output")
    p_rec.set_defaults(func=cmd_recommend, json=False)

    p_alert = sub.add_parser("alert", help="manage price alerts")
    alert_sub = p_alert.add_subparsers(dest="alert_command", required=True)
    p_aa = alert_sub.add_parser(
        "add", help="e.g.: alert add NVDA below 150 | alert add MSFT drop 15")
    p_aa.add_argument("symbol")
    p_aa.add_argument("kind", choices=["above", "below", "drop", "day"])
    p_aa.add_argument("value", type=float,
                      help="price for above/below, percent for drop/day")
    p_aa.set_defaults(func=cmd_alert_add)
    p_ar = alert_sub.add_parser("remove", help="remove by alert id or symbol")
    p_ar.add_argument("target")
    p_ar.set_defaults(func=cmd_alert_remove)
    p_al = alert_sub.add_parser("list", help="show configured alerts")
    p_al.set_defaults(func=cmd_alert_list)

    p_check = sub.add_parser(
        "alerts", help="check all alerts now (exit code 2 when any trigger)")
    p_check.add_argument("--verbose", action="store_true",
                         help="also show alerts that did not trigger")
    p_check.set_defaults(func=cmd_alerts_check)

    args = parser.parse_args(argv)
    if not args.command:
        args.func = cmd_watch
        args.json = False
    return args.func(args)
