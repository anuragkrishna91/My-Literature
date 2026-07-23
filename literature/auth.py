"""Authenticated fetch via a reused browser session — never a stored password.

The tool opens a real browser with a *persistent profile*. You log in through
your institution yourself (SSO, MFA, whatever your campus uses), exactly as you
would by hand. The session cookies live in the profile directory, so subsequent
runs reuse them and you only log in occasionally.

The tool never sees, types, or stores your password. It only reuses a session
you established. This is what makes authenticated access here both workable
(MFA/SSO just work) and safe (no credential to leak).

Playwright is imported lazily so the open-access path has zero heavyweight
dependencies. Install it only if you use this module:
    pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import os
import time
from typing import Optional

from .config import Config
from .resolve import Candidate, publisher_landing_url


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "The authenticated path needs Playwright, which isn't installed.\n"
            "  pip install playwright && python -m playwright install chromium\n"
            "Or drop --allow-auth to stay on open-access sources only."
        ) from exc
    from playwright.sync_api import sync_playwright
    return sync_playwright


def ensure_logged_in(cfg: Config, login_url: Optional[str] = None) -> None:
    """Open a browser so the user can establish/refresh their session.

    Blocks until the user confirms they've finished logging in. The resulting
    cookies persist in cfg.browser_profile_dir for reuse.
    """
    sync_playwright = _require_playwright()
    os.makedirs(cfg.browser_profile_dir, exist_ok=True)
    start = login_url or "https://www.google.com/scholar"
    print(
        "\nOpening a browser. Log in through your institution as you normally "
        "would\n(including any MFA). The tool never sees your password — it only "
        "reuses\nthe session you create. Close the browser window when you're "
        "signed in.\n"
    )
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            cfg.browser_profile_dir, headless=False,
            user_agent=cfg.user_agent,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(start, wait_until="domcontentloaded")
        except Exception:
            pass
        # Wait for the human to close the window, signalling they're done.
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            _safe_close(ctx)


def fetch_with_session(cand: Candidate, cfg: Config) -> Optional[str]:
    """Attempt an authenticated download for a paywalled paper.

    Uses the persisted session cookies. Returns a file path on success, or None
    if the paper still isn't reachable (in which case you likely don't have
    institutional access to it, and the tool stops rather than pushing further).
    """
    sync_playwright = _require_playwright()
    if not os.path.isdir(cfg.browser_profile_dir):
        ensure_logged_in(cfg)

    landing = publisher_landing_url(cand.doi) if cand.doi else cand.pdf_url
    os.makedirs(cfg.out_dir, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            cfg.browser_profile_dir, headless=True,
            user_agent=cfg.user_agent, accept_downloads=True,
        )
        try:
            page = ctx.new_page()
            page.goto(landing, wait_until="domcontentloaded",
                      timeout=int(cfg.timeout * 1000))
            # Politeness: one deliberate pause before probing for the PDF link.
            time.sleep(cfg.min_request_interval)
            pdf_href = _find_pdf_link(page)
            if not pdf_href:
                return None
            path = _download_via_browser(ctx, page, pdf_href, cand, cfg)
            return path
        finally:
            _safe_close(ctx)


def _find_pdf_link(page) -> Optional[str]:
    """Look for a direct-PDF link on the article landing page."""
    # Publishers commonly expose the PDF via a citation meta tag.
    meta = page.query_selector('meta[name="citation_pdf_url"]')
    if meta:
        href = meta.get_attribute("content")
        if href:
            return href
    for sel in ('a[href$=".pdf"]', 'a[href*="/pdf"]', 'a:has-text("PDF")'):
        el = page.query_selector(sel)
        if el:
            href = el.get_attribute("href")
            if href:
                return href
    return None


def _download_via_browser(ctx, page, pdf_href: str, cand: Candidate,
                          cfg: Config) -> Optional[str]:
    from .download import _filename_for, _file_is_pdf

    dest = os.path.join(cfg.out_dir, _filename_for(cand))
    abs_url = pdf_href if pdf_href.startswith("http") else page.url
    try:
        with page.expect_download(timeout=int(cfg.timeout * 1000)) as dl_info:
            page.goto(abs_url)
        dl_info.value.save_as(dest)
    except Exception:
        # Some PDFs render inline instead of triggering a download event.
        resp = ctx.request.get(abs_url)
        if not resp.ok:
            return None
        with open(dest, "wb") as fh:
            fh.write(resp.body())
    if not _file_is_pdf(dest):
        if os.path.exists(dest):
            os.remove(dest)
        return None
    return dest


def _safe_close(ctx) -> None:
    try:
        ctx.close()
    except Exception:
        pass
