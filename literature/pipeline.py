"""Orchestrates resolve -> open-access download -> authenticated fallback."""

from __future__ import annotations

from typing import Optional

from .config import Config
from .download import DownloadResult, download_first_available
from .http import PoliteSession
from .resolve import Candidate, looks_like_doi, resolve, doi_from_title


def fetch_one(query: str, session: PoliteSession, cfg: Config) -> DownloadResult:
    """Resolve and download a single paper, honoring the open-access-first policy."""
    candidates = resolve(query, session, cfg.email)
    result = download_first_available(query, candidates, session, cfg)

    # Only reach for the authenticated path when open access genuinely missed
    # and the user has opted in.
    if result.status in ("not_found", "error") and cfg.allow_auth:
        auth_result = _try_authenticated(query, session, cfg)
        if auth_result is not None:
            return auth_result

    if result.status in ("not_found", "error") and not cfg.allow_auth:
        result.detail += (
            " (No open-access copy. Re-run with --allow-auth to try your "
            "institutional session.)"
        )
    return result


def _try_authenticated(query: str, session: PoliteSession,
                       cfg: Config) -> Optional[DownloadResult]:
    # Lazy import so open-access-only users never load Playwright.
    from .auth import fetch_with_session

    doi = query if looks_like_doi(query) else doi_from_title(query, session, cfg.email)
    if not doi:
        return DownloadResult(query, None, None, "not_found",
                              "Could not resolve a DOI for the authenticated path.")
    cand = Candidate(pdf_url="", source="authenticated", is_open_access=False, doi=doi)
    path = fetch_with_session(cand, cfg)
    if path:
        from .download import _record_metadata
        _record_metadata(cand, path, cfg)
        return DownloadResult(query, path, "authenticated", "downloaded")
    return DownloadResult(
        query, None, "authenticated", "paywalled",
        "Not reachable even with your session — you may not have institutional "
        "access to this paper. Stopping here rather than pushing further.",
    )
