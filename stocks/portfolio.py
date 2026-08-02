"""Personal portfolio valuation.

You record what you actually bought on Degiro in a small CSV (see
``portfolio.example.csv``): one row per position with the ticker, number of
shares, and your average purchase price in USD. Degiro shows the average
price per position under Portfolio -> (position) -> "GAK"/"BEP". This file
stays local — it is gitignored by default.
"""

import csv
import os


class PortfolioError(Exception):
    pass


class Holding:
    def __init__(self, symbol, shares, cost_per_share):
        self.symbol = symbol.upper()
        self.shares = shares
        self.cost_per_share = cost_per_share

    @property
    def cost_basis(self):
        return self.shares * self.cost_per_share

    def market_value(self, price):
        return self.shares * price

    def gain(self, price):
        return self.market_value(price) - self.cost_basis

    def gain_pct(self, price):
        if self.cost_basis == 0:
            return None
        return self.gain(price) / self.cost_basis * 100.0


def load_portfolio(path):
    """Read holdings from CSV with columns: symbol, shares, cost_per_share_usd.
    Blank lines and lines starting with '#' are ignored."""
    if not os.path.exists(path):
        raise PortfolioError(
            "portfolio file not found: %s (copy portfolio.example.csv to get started)"
            % path
        )
    holdings = []
    with open(path, "r", encoding="utf-8") as fh:
        rows = [line for line in fh if line.strip() and not line.lstrip().startswith("#")]
    reader = csv.DictReader(rows)
    required = {"symbol", "shares", "cost_per_share_usd"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise PortfolioError(
            "portfolio CSV needs columns: symbol, shares, cost_per_share_usd"
        )
    for line_no, row in enumerate(reader, start=2):
        try:
            holdings.append(
                Holding(
                    row["symbol"].strip(),
                    float(row["shares"]),
                    float(row["cost_per_share_usd"]),
                )
            )
        except (TypeError, ValueError):
            raise PortfolioError("bad number on portfolio row %d: %r" % (line_no, row))
    if not holdings:
        raise PortfolioError("portfolio file has no holdings: %s" % path)
    return holdings


def value_portfolio(holdings, prices):
    """Combine holdings with current prices.

    Returns (rows, totals) where each row is a dict per holding (price is
    None when the quote failed) and totals sums only the priced holdings.
    """
    rows = []
    total_cost = total_value = 0.0
    for holding in holdings:
        price = prices.get(holding.symbol)
        row = {
            "symbol": holding.symbol,
            "shares": holding.shares,
            "cost_per_share": holding.cost_per_share,
            "cost_basis": holding.cost_basis,
            "price": price,
            "value": None,
            "gain": None,
            "gain_pct": None,
        }
        if price is not None:
            row["value"] = holding.market_value(price)
            row["gain"] = holding.gain(price)
            row["gain_pct"] = holding.gain_pct(price)
            total_cost += holding.cost_basis
            total_value += row["value"]
        rows.append(row)
    totals = {
        "cost_basis": total_cost,
        "value": total_value,
        "gain": total_value - total_cost,
        "gain_pct": ((total_value - total_cost) / total_cost * 100.0)
        if total_cost else None,
    }
    return rows, totals
