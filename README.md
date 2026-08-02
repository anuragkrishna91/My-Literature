# My-Literature

A polite, resolver-first tool for downloading academic papers by DOI or title.

It fetches the papers **you already have a right to read** — through legal open-access
sources first, and through an authenticated browser session for the rest — without
hammering journal sites or storing your university password.

## Design philosophy

Publishers actively monitor for automated bulk downloading and will suspend an
entire institution's access when they detect it. This tool is deliberately built
to stay well under that line:

1. **Open access first.** For every DOI it checks Unpaywall, arXiv, and PubMed
   Central before ever touching a paywall. A large share of papers have a legal
   free copy, and these sources have real APIs built for automation.
2. **Authenticated access via *your* browser session, not your password.** For the
   paywalled remainder, you log in once in a real browser window (clearing SSO/MFA
   yourself), and the tool reuses that session's cookies. It never sees or stores
   your credentials.
3. **Polite by default.** Hard rate limiting, a real User-Agent, retries with
   backoff, and a per-run cap. It is meant for keeping a personal library current —
   tens of papers over time — not for scraping journal archives.

If you need to build a large full-text corpus for text mining, don't use this —
use your publisher's official **Text & Data Mining (TDM) API**, which is free to
subscribers and is the sanctioned route for that. See `docs/tdm-apis.md`.

## Install

```bash
pip install -r requirements.txt

# Only needed if you'll use the authenticated (paywall) path:
python -m playwright install chromium
```

`requests` is the only hard dependency for open-access downloads. Playwright is
imported lazily and only required when you use `--allow-auth`.

## Usage

```bash
# Set a contact email (required by Unpaywall/Crossref etiquette)
export LITERATURE_EMAIL="you@university.edu"

# Download a single paper by DOI (open-access sources only)
python -m literature get 10.1038/s41586-020-2649-2

# By title (resolves to a DOI first)
python -m literature get --title "Attention is all you need"

# From a list, one DOI or title per line
python -m literature batch papers.txt --out ./library

# Allow the authenticated browser fallback for paywalled papers.
# Opens a browser the first time so you can log in via your institution.
python -m literature get 10.1016/j.cell.2021.01.001 --allow-auth
```

Downloaded PDFs and a `metadata.jsonl` index land in the output directory
(default `./library`).

## Embedding in another app (RAG / reference manager)

The downloader is importable, not just a CLI. If you have a paper-RAG app whose
search already returns OpenAlex results and whose corpus is fed by an indexer,
you can drop this in between:

```python
from literature.api import download_openalex_works, Progress

results = download_openalex_works(
    works,                    # OpenAlex work dicts your search already returned
    out_dir=corpus_pdf_dir,   # same folder your other PDFs live in
    email="you@university.edu",
    allow_auth=False,         # True to try your institutional session for paywalled
)
new_pdfs = [r.path for r in results if r.status == "downloaded"]
# ...then run your existing chunk-and-index step over new_pdfs.
```

See `docs/integrate-rag.md` for the full wiring (e.g. a "Download OA PDFs into
corpus" button alongside a Zotero sync).

## Stock tracker (`stocks/`)

A separate small tool in this repo for tracking US-listed software, AI,
biotech, and medicine stocks — a companion to a Degiro account. It uses free
public quote endpoints (Yahoo Finance, falling back to Stooq), needs no API
key, and only requires `requests`.

```bash
# Show the sector watchlist: price, day/1-month/YTD change, 52-week range,
# distance from the 52-week high, and 50/200-day trend.
python -m stocks watch          # or just: python -m stocks
python -m stocks watch --json   # machine-readable

# See or edit the watchlist (edits are saved to stocks_watchlist.json)
python -m stocks list
python -m stocks add SHOP --sector "Software" --name "Shopify"
python -m stocks remove MRNA

# Value your actual Degiro holdings:
#   cp portfolio.example.csv portfolio.csv   # then fill in your positions
python -m stocks portfolio

# Rank the watchlist by mechanical trend/momentum/RSI signals, with the
# reasoning for every point of the score printed out.
python -m stocks recommend

# Price alerts (saved to stocks_alerts.json):
python -m stocks alert add NVDA below 150   # buy-the-dip target price
python -m stocks alert add MSFT above 600   # breakout / take-profit price
python -m stocks alert add AMGN drop 15     # 15%+ below its 52-week high
python -m stocks alert add PLTR day 5       # daily move of 5%+ either way
python -m stocks alert list
python -m stocks alert remove 3             # by id, or by symbol
python -m stocks alerts                     # check them all now
python -m stocks alerts --verbose           # also show the quiet ones
```

`alerts` exits with code 2 when anything triggers, so you can wire it into
cron or a shell loop for notifications, e.g.
`python -m stocks alerts || notify-send "stock alert"`.

The default watchlist covers Software (MSFT, GOOGL, ORCL, CRM, ADBE, NOW),
AI & Semiconductors (NVDA, AMD, AVGO, TSM, PLTR, ARM), Biotech (AMGN, VRTX,
REGN, GILD, MRNA, CRSP), and Medicine & Healthcare (LLY, NVO, JNJ, MRK,
ABBV, ISRG, UNH). For the portfolio, record each position's ticker, share
count, and your average USD purchase price (Degiro shows this as the
position's break-even/GAK price); the tool prints per-position and total
gain in USD, plus totals in EUR at the live EURUSD rate. `portfolio.csv`
and `stocks_watchlist.json` are gitignored so your personal data stays off
GitHub.

The watch table also shows each stock's 14-day RSI (classically, below 30
is "oversold" and above 70 "overbought") next to the 50/200-day trend.
`recommend` combines those same signals — trend, 1-month and YTD momentum,
distance from the 52-week high, RSI — into a transparent score where every
point comes with a printed reason.

This is an informational tracker, not investment advice — the
"recommendations" are mechanical technical signals, quotes are
end-of-day/delayed, and none of it knows your finances or risk tolerance.

## What this tool will not do

- Store or type your university password.
- Bypass a paywall you don't have legitimate access to.
- Download faster than the configured rate limit or above the per-run cap.
- Scrape a journal's full archive.

These aren't arbitrary — they're the difference between a tool that keeps working
and one that gets your whole campus's access revoked.
