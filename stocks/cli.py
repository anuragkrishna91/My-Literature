"""Command-line interface: watch, portfolio, add, remove, list."""

import argparse
import json
import sys

from .portfolio import PortfolioError, load_portfolio, value_portfolio
from .quotes import fetch_eur_usd, fetch_quotes
from .watchlist import (WATCHLIST_FILE, add_symbol, all_symbols,
                        load_watchlist, remove_symbol)


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
                "trend": q.trend,
            })
        print(json.dumps(out, indent=2))
    else:
        headers = ["Company", "Sym", "Price", "Day", "1M", "YTD",
                   "52w Low", "52w High", "Off High", "Trend"]
        for sector in watchlist:
            entries = [(s, n) for sec, s, n in flat if sec == sector]
            sector_rows = []
            day_moves = []
            for symbol, name in entries:
                q = quotes.get(symbol)
                if not q:
                    sector_rows.append([name, symbol] + ["-"] * 8)
                    continue
                if q.day_change_pct is not None:
                    day_moves.append(q.day_change_pct)
                sector_rows.append([
                    name, symbol, _fmt(q.last), _fmt_pct(q.day_change_pct),
                    _fmt_pct(q.month_change_pct), _fmt_pct(q.ytd_change_pct),
                    _fmt(q.low_52w), _fmt(q.high_52w),
                    _fmt_pct(q.pct_off_high), q.trend or "-",
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

    args = parser.parse_args(argv)
    if not args.command:
        args.func = cmd_watch
        args.json = False
    return args.func(args)
