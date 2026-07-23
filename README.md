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

## What this tool will not do

- Store or type your university password.
- Bypass a paywall you don't have legitimate access to.
- Download faster than the configured rate limit or above the per-run cap.
- Scrape a journal's full archive.

These aren't arbitrary — they're the difference between a tool that keeps working
and one that gets your whole campus's access revoked.
