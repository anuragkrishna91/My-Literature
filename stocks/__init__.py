"""Stock tracker for US-listed software, AI, biotech, and medicine companies.

A small, key-free tracker meant to accompany a Degiro (or any broker)
account: it watches a curated list of US tickers by sector and can value a
personal portfolio you record in a CSV. Quotes come from free public
endpoints (Yahoo Finance chart API, with Stooq as fallback) — no API key,
no scraping of your broker.
"""

__version__ = "0.1.0"
