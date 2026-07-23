# Official Text & Data Mining (TDM) APIs

If you ever need more than a personal library's worth of papers — a full-text
corpus for text mining, systematic review at scale, etc. — do **not** point this
tool at a journal and turn up the volume. Use the publisher's official TDM API
instead. They are free to subscribers, return clean full text, and are the
sanctioned route for programmatic access. Bulk scraping is the route that gets an
institution's access suspended.

## Why this is the right path

- The publisher *expects* the traffic and won't flag your account or your
  campus's IP range.
- You get structured full text (often XML/JATS), not scraped HTML.
- An API key is tied to your institutional entitlement, so you only ever get what
  you're licensed for — no gray area.

## Where to get a key

| Publisher | Programmatic access | Notes |
|-----------|--------------------|-------|
| Elsevier / ScienceDirect | https://dev.elsevier.com | Free API key; full-text TDM for subscribers. |
| Springer Nature | https://dev.springernature.com | Metadata + full-text APIs; free tier + TDM. |
| Wiley | Wiley TDM (via your library) | Token-based full-text PDF retrieval. |
| IEEE Xplore | https://developer.ieee.org | Metadata API; full text per subscription. |
| Crossref | https://www.crossref.org/documentation/retrieve-metadata/rest-api/ | Metadata + TDM *links* for the whole corpus, no key. |

Start by asking your university library — they manage the institutional
entitlements and can usually issue or authorize a TDM key quickly.

## Open-access sources (no key, already used by this tool)

- **Unpaywall** — https://unpaywall.org/products/api
- **arXiv** — https://info.arxiv.org/help/api/
- **PubMed Central OA** — https://www.ncbi.nlm.nih.gov/pmc/tools/oa-service/
- **CORE** — https://core.ac.uk/services/api
- **Semantic Scholar** — https://api.semanticscholar.org

A resolver-first tool like this one already gets you a large fraction of most
reading lists from these alone.
