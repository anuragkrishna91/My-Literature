"""Drop-in bridge between Manuscript Workbench and the My-Literature downloader.

Copy this file *and* the `literature/` package folder into your Paper rag
directory (next to workbench.py). Then wire the three functions below into the
"Get Papers" tab (see docs/workbench-integration.md).

What it adds over the app's existing get_papers.download_oa_pdfs:
  * Robust OA resolution — falls back through Unpaywall / arXiv / PubMed Central
    when OpenAlex's pdf_url is dead ("dead OA links are common"), and verifies
    the downloaded bytes are a real PDF, not a publisher login page.
  * An authenticated path for the paywalled (lock) items, using a browser
    session YOU establish through UHasselt SSO — the tool never sees your
    password. This is the piece the DOI-list export currently leaves manual.

All functions return (n_ok, n_fail, messages) to match the app's existing
download_oa_pdfs contract, and accept the same callback(i, total, title).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from literature.api import Progress, download_records
from literature.config import Config

# ===========================================================================
# YOUR LIBRARY SETTINGS — edit these two lines for your institution.
# ===========================================================================
# The page opened when you click "Set up / refresh login". Log in here.
INSTITUTION_LOGIN_URL = (
    "https://tl3ry3ge2c.search.serialssolutions.com/ejp/?libHash=TL3RY3GE2C#/?language=en-US"
)
# A URL prefix that resolves a DOI to full text through your library. The tool
# appends the DOI to this. For SerialsSolutions/360 Link this is the OpenURL
# endpoint on your library's resolver host.
RESOLVER_OPENURL_BASE = (
    "https://tl3ry3ge2c.search.serialssolutions.com/?rft_id=info:doi/"
)
# ===========================================================================

# Subfolders under PDF_DIR so ingest.py picks the PDFs up and they don't collide
# with the app's own OpenAlex or Zotero downloads.
OA_SUBDIR = "openalex_resolved"
AUTH_SUBDIR = "institutional"

Callback = Callable[[int, int, str], None]


def _run(records: Sequence[dict], out_dir: str, email: str, allow_auth: bool,
         callback: Callback, min_interval: float) -> Tuple[int, int, List[str]]:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    total = len(records)
    state = {"i": 0}
    msgs: List[str] = []

    def on_item(result):
        state["i"] += 1
        callback(state["i"], total, result.query or "")
        tag = {"downloaded": "OK", "paywalled": "paywalled",
               "not_found": "no OA copy", "error": "failed"}.get(
                   result.status, result.status)
        line = f"[{tag}] {result.query}"
        if result.source:
            line += f" ({result.source})"
        if result.detail and result.status != "downloaded":
            line += f" - {result.detail}"
        msgs.append(line)

    results = download_records(
        records, out_dir=out_dir, email=email, allow_auth=allow_auth,
        min_request_interval=min_interval, max_per_run=max(total, 1),
        institution_login_url=INSTITUTION_LOGIN_URL,
        resolver_openurl_base=RESOLVER_OPENURL_BASE,
        progress=Progress(on_item=on_item),
    )
    n_ok = sum(1 for r in results if r.status == "downloaded")
    return n_ok, total - n_ok, msgs


def download_oa_resolved(records: Sequence[dict], pdf_dir: str, email: str,
                         callback: Callback) -> Tuple[int, int, List[str]]:
    """Download OA PDFs with full fallback resolution. No credentials used.

    Pass the same OpenAlex result records the app already holds
    (dicts with 'pdf_url', 'doi', 'title'). Open-access sources only.
    """
    out = str(Path(pdf_dir) / OA_SUBDIR)
    return _run(records, out, email, allow_auth=False, callback=callback,
                min_interval=2.0)


def download_paywalled_via_session(records: Sequence[dict], pdf_dir: str,
                                   email: str, callback: Callback
                                   ) -> Tuple[int, int, List[str]]:
    """Fetch the paywalled (non-OA) items via your institutional browser session.

    Only records without an OA PDF are attempted. Requires that you've run
    setup_login() at least once so a session exists. Conservatively rate-limited;
    meant for small batches of papers you have UHasselt access to.
    """
    paywalled = [r for r in records if not r.get("pdf_url")]
    if not paywalled:
        return 0, 0, ["Nothing to do: every result already has an OA PDF."]
    out = str(Path(pdf_dir) / AUTH_SUBDIR)
    return _run(paywalled, out, email, allow_auth=True, callback=callback,
                min_interval=5.0)


def setup_login(email: str) -> None:
    """Open your library login page so you can sign in once; the session is
    reused for later paywalled downloads. Never stores your password."""
    from literature.auth import ensure_logged_in
    cfg = Config(email=email, institution_login_url=INSTITUTION_LOGIN_URL,
                 resolver_openurl_base=RESOLVER_OPENURL_BASE)
    ensure_logged_in(cfg, login_url=INSTITUTION_LOGIN_URL)
