"""Programmatic API for embedding the downloader in another app (e.g. a RAG UI).

Everything the CLI does is available here as plain function calls that return
structured results, so a host application can drive downloads and then hand the
resulting PDF paths to its own indexer.

Typical wiring in a RAG app whose search already uses OpenAlex:

    from literature.api import download_openalex_works, Progress

    results = download_openalex_works(
        works,                       # the list of OpenAlex work dicts from search
        out_dir=corpus_pdf_dir,      # same folder your Zotero PDFs land in
        email="you@university.edu",
        allow_auth=False,            # True to try institutional session for paywalled
        progress=Progress(on_item=lambda r: print(r.query, r.status)),
    )
    new_pdfs = [r.path for r in results if r.status == "downloaded"]
    # ...then run your existing chunk-and-index step over new_pdfs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from .config import Config
from .download import DownloadResult, download_first_available
from .http import PoliteSession
from .pipeline import fetch_one
from .resolve import Candidate, candidates_from_openalex_work


@dataclass
class Progress:
    """Optional callbacks so a UI can show live progress and stay responsive."""
    on_item: Optional[Callable[[DownloadResult], None]] = None
    # Return False from should_continue to stop early (e.g. user hit Cancel).
    should_continue: Optional[Callable[[], bool]] = None


def _make_session(cfg: Config) -> PoliteSession:
    return PoliteSession(cfg.user_agent, cfg.min_request_interval,
                         cfg.timeout, cfg.max_retries)


def _run(items: Sequence, handler, cfg: Config,
         progress: Optional[Progress]) -> List[DownloadResult]:
    if len(items) > cfg.max_per_run:
        raise ValueError(
            f"{len(items)} items exceeds the per-run cap of {cfg.max_per_run}. "
            f"Raise Config.max_per_run deliberately if you really mean to.")
    session = _make_session(cfg)
    results: List[DownloadResult] = []
    try:
        for item in items:
            if progress and progress.should_continue and not progress.should_continue():
                break
            result = handler(item, session)
            results.append(result)
            if progress and progress.on_item:
                progress.on_item(result)
    finally:
        session.close()
    return results


def download_dois(dois: Sequence[str], *, out_dir: str, email: str,
                  allow_auth: bool = False,
                  min_request_interval: float = 3.0,
                  max_per_run: int = 50,
                  progress: Optional[Progress] = None) -> List[DownloadResult]:
    """Resolve and download a list of DOIs (or titles) into out_dir."""
    cfg = Config(email=email, out_dir=out_dir, allow_auth=allow_auth,
                 min_request_interval=min_request_interval, max_per_run=max_per_run)
    return _run(list(dois), lambda q, s: fetch_one(q, s, cfg), cfg, progress)


def download_openalex_works(works: Sequence[dict], *, out_dir: str, email: str,
                            allow_auth: bool = False,
                            min_request_interval: float = 3.0,
                            max_per_run: int = 50,
                            progress: Optional[Progress] = None
                            ) -> List[DownloadResult]:
    """Download PDFs for OpenAlex work objects a search step already returned.

    Uses the OA PDF url carried in each work when present (no extra lookup), and
    falls back to full DOI resolution (Unpaywall/arXiv/PMC) when a work has no
    direct PDF but does have a DOI. Honors allow_auth for the paywalled remainder.
    """
    cfg = Config(email=email, out_dir=out_dir, allow_auth=allow_auth,
                 min_request_interval=min_request_interval, max_per_run=max_per_run)

    def handle(work: dict, session: PoliteSession) -> DownloadResult:
        label = work.get("display_name") or work.get("doi") or work.get("id") or "?"
        cands = candidates_from_openalex_work(work)
        if cands:
            result = download_first_available(label, cands, session, cfg)
            if result.status == "downloaded":
                return result
        # No direct OA PDF (or it failed): fall back to DOI resolution + auth path.
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            return fetch_one(doi, session, cfg)
        return DownloadResult(label, None, None, "not_found",
                              "No OA PDF in the OpenAlex record and no DOI to resolve.")

    return _run(list(works), handle, cfg, progress)


def download_records(records: Sequence[dict], *, out_dir: str, email: str,
                     allow_auth: bool = False,
                     min_request_interval: float = 3.0,
                     max_per_run: int = 100,
                     institution_login_url: Optional[str] = None,
                     ezproxy_login_prefix: Optional[str] = None,
                     resolver_openurl_base: Optional[str] = None,
                     progress: Optional[Progress] = None) -> List[DownloadResult]:
    """Download from lightweight record dicts: {pdf_url?, doi?, title?}.

    This matches the shape a search UI often already holds (e.g. the workbench's
    ``search_openalex`` results), so it can be called without reshaping data.

    Strategy per record: try the record's own ``pdf_url`` first; on failure fall
    back to DOI resolution (Unpaywall/OpenAlex/arXiv/PMC), and, when
    ``allow_auth`` is set, the authenticated institutional session for anything
    still behind a paywall. ``resolver_openurl_base`` routes that authenticated
    fetch through a library link resolver.
    """
    cfg = Config(email=email, out_dir=out_dir, allow_auth=allow_auth,
                 min_request_interval=min_request_interval, max_per_run=max_per_run,
                 institution_login_url=institution_login_url,
                 ezproxy_login_prefix=ezproxy_login_prefix,
                 resolver_openurl_base=resolver_openurl_base)

    def handle(rec: dict, session: PoliteSession) -> DownloadResult:
        doi = (rec.get("doi") or "").replace("https://doi.org/", "") or None
        title = rec.get("title")
        label = title or doi or "?"
        pdf_url = rec.get("pdf_url")
        if pdf_url:
            cand = Candidate(pdf_url, "openalex", True, doi=doi, title=title)
            result = download_first_available(label, [cand], session, cfg)
            if result.status == "downloaded":
                return result
        if doi:
            return fetch_one(doi, session, cfg)
        return DownloadResult(label, None, None, "not_found",
                              "No working PDF url and no DOI to resolve.")

    return _run(list(records), handle, cfg, progress)
