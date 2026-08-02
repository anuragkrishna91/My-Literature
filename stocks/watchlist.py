"""Curated default watchlist by sector, plus a user-editable overlay.

The defaults cover large, liquid US-listed names (all tradeable on Degiro
via NASDAQ/NYSE). Edits made with ``add``/``remove`` are saved to
``stocks_watchlist.json`` in the working directory, which then fully
replaces the defaults on later runs.
"""

import json
import os

WATCHLIST_FILE = "stocks_watchlist.json"

# sector -> list of (symbol, company name)
DEFAULT_WATCHLIST = {
    "Software": [
        ("MSFT", "Microsoft"),
        ("GOOGL", "Alphabet"),
        ("ORCL", "Oracle"),
        ("CRM", "Salesforce"),
        ("ADBE", "Adobe"),
        ("NOW", "ServiceNow"),
    ],
    "AI & Semiconductors": [
        ("NVDA", "NVIDIA"),
        ("AMD", "AMD"),
        ("AVGO", "Broadcom"),
        ("TSM", "TSMC (ADR)"),
        ("PLTR", "Palantir"),
        ("ARM", "Arm Holdings (ADR)"),
    ],
    "Biotech": [
        ("AMGN", "Amgen"),
        ("VRTX", "Vertex Pharmaceuticals"),
        ("REGN", "Regeneron"),
        ("GILD", "Gilead Sciences"),
        ("MRNA", "Moderna"),
        ("CRSP", "CRISPR Therapeutics"),
    ],
    "Medicine & Healthcare": [
        ("LLY", "Eli Lilly"),
        ("NVO", "Novo Nordisk (ADR)"),
        ("JNJ", "Johnson & Johnson"),
        ("MRK", "Merck"),
        ("ABBV", "AbbVie"),
        ("ISRG", "Intuitive Surgical"),
        ("UNH", "UnitedHealth"),
    ],
}


def load_watchlist(path=WATCHLIST_FILE):
    """Return the effective watchlist: the saved user file if present,
    otherwise the built-in defaults."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {sector: [tuple(entry) for entry in entries]
                for sector, entries in raw.items()}
    return {sector: list(entries) for sector, entries in DEFAULT_WATCHLIST.items()}


def save_watchlist(watchlist, path=WATCHLIST_FILE):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({sector: [list(entry) for entry in entries]
                   for sector, entries in watchlist.items()},
                  fh, indent=2)


def add_symbol(symbol, sector, name=None, path=WATCHLIST_FILE):
    """Add a ticker to a sector (creating the sector if new). Returns the
    updated watchlist."""
    symbol = symbol.upper()
    watchlist = load_watchlist(path)
    entries = watchlist.setdefault(sector, [])
    if any(sym == symbol for sym, _ in entries):
        return watchlist
    entries.append((symbol, name or symbol))
    save_watchlist(watchlist, path)
    return watchlist


def remove_symbol(symbol, path=WATCHLIST_FILE):
    """Remove a ticker from whichever sector holds it. Returns True if it
    was found."""
    symbol = symbol.upper()
    watchlist = load_watchlist(path)
    found = False
    for sector in list(watchlist):
        kept = [(sym, name) for sym, name in watchlist[sector] if sym != symbol]
        if len(kept) != len(watchlist[sector]):
            found = True
            if kept:
                watchlist[sector] = kept
            else:
                del watchlist[sector]
    if found:
        save_watchlist(watchlist, path)
    return found


def all_symbols(watchlist):
    """Flatten to a list of (sector, symbol, name), preserving order."""
    flat = []
    for sector, entries in watchlist.items():
        for symbol, name in entries:
            flat.append((sector, symbol, name))
    return flat
