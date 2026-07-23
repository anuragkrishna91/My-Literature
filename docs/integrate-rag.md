# Wiring the downloader into a paper-RAG app (e.g. Manuscript Workbench)

Your app already has the two pieces this connects: an **OpenAlex search** that
returns work objects, and a **corpus + indexer** that Zotero PDFs feed into. The
downloader is the missing middle — it turns search results into PDFs on disk,
which then go through your *existing* chunk-and-index step. No new indexing code.

```
OpenAlex search ──► [download_openalex_works] ──► PDFs in corpus folder ──► your indexer
   (you have)              (this package)              (same as Zotero)      (you have)
```

## The call

Add a "Download OA PDFs into corpus" button next to "Sync Zotero PDFs into
corpus". Its handler:

```python
from literature.api import download_openalex_works, Progress

def on_download_search_results(works, corpus_pdf_dir, email, reindex, log):
    """works: the list of OpenAlex work dicts your search already produced.
       corpus_pdf_dir: the same folder your Zotero PDFs land in.
       reindex / log: your app's existing index + status-line functions."""
    results = download_openalex_works(
        works,
        out_dir=corpus_pdf_dir,
        email=email,                 # reuse the contact email you already ask for
        allow_auth=False,            # flip to True to try the institutional session
        min_request_interval=3.0,    # polite; keeps you off publisher radar
        max_per_run=25,              # matches your "Max results" slider nicely
        progress=Progress(on_item=lambda r: log(f"{r.status}: {r.query}")),
    )
    new_pdfs = [r.path for r in results if r.status == "downloaded"]
    if new_pdfs:
        reindex()                    # your existing "re-index" over the corpus folder
    return results                   # show a per-paper summary in the UI
```

`download_openalex_works` uses the OA PDF url each work already carries (no extra
lookup), and only falls back to DOI resolution (Unpaywall → OpenAlex →
arXiv → PMC) when a work has no direct PDF. Set `allow_auth=True` and it will try
your reused institutional browser session for the paywalled remainder.

## Result objects

Each element is a `DownloadResult` with:

| field | meaning |
|-------|---------|
| `query` | the paper's title or DOI |
| `status` | `downloaded` \| `paywalled` \| `not_found` \| `error` |
| `path` | absolute path to the saved PDF (when `downloaded`) |
| `source` | which source served it (`openalex`, `unpaywall`, `arxiv`, `pmc`, `authenticated`) |
| `detail` | human-readable note for the non-downloaded cases |

Render `status` per row so the user sees, e.g., "18 downloaded, 4 paywalled,
3 no OA copy" — the paywalled/not-found rows are exactly the ones a human might
grab manually or via your Zotero Connector.

## Why route through this instead of downloading in the app directly

OpenAlex's `oa_url` is sometimes a landing page, not a PDF; publishers return
200-with-HTML login pages that look like a success. This package verifies the
`%PDF-` bytes and discards those, so your index never fills with junk "PDFs".
It also centralizes rate limiting and the per-run cap, and gives you the
authenticated fallback for free — all the parts that are fiddly to get right and
easy to get *dangerously* wrong if reimplemented inline.

## De-duplication note

Point `out_dir` at your corpus folder and the tool names files by DOI, so
re-running a search won't create duplicates for papers you already have — the
same filename is overwritten with an identical PDF. If your indexer keys on file
path, no duplicate chunks result. (If it keys on content hash, you're already
covered.)
