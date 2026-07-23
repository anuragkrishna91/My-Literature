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


def _clear_stale_locks(profile_dir: str) -> None:
    """Remove Chromium singleton lock files left behind by a crashed or
    force-closed browser. Safe to delete when no live Chromium holds the
    profile; if one does, the relaunch still fails and we surface a clear
    message rather than this cryptic Playwright error."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = os.path.join(profile_dir, name)
        try:
            if os.path.lexists(p):
                os.remove(p)
        except OSError:
            pass


def _launch_persistent(pw, cfg: Config, **kwargs):
    """Launch a persistent browser context, self-healing a stale profile lock.

    The common failure ("profile is already in use") is usually an orphaned
    Chromium from a previous run, not a genuinely concurrent browser. Clear the
    lock files and retry once; if it still fails, raise an actionable message.
    """
    try:
        return pw.chromium.launch_persistent_context(
            cfg.browser_profile_dir, user_agent=cfg.user_agent, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if "already in use" not in str(exc).lower():
            raise
        _clear_stale_locks(cfg.browser_profile_dir)
        try:
            return pw.chromium.launch_persistent_context(
                cfg.browser_profile_dir, user_agent=cfg.user_agent, **kwargs)
        except Exception:
            raise RuntimeError(
                "The browser profile is still locked by a Chromium that's "
                "running. Close every browser window the tool opened (or just "
                "restart your computer), then try again. If it keeps happening, "
                "delete this folder and log in once more:\n  "
                f"{cfg.browser_profile_dir}"
            ) from exc


def ensure_logged_in(cfg: Config, login_url: Optional[str] = None) -> None:
    """Open a browser so the user can establish/refresh their session.

    Blocks until the user confirms they've finished logging in. The resulting
    cookies persist in cfg.browser_profile_dir for reuse.
    """
    sync_playwright = _require_playwright()
    os.makedirs(cfg.browser_profile_dir, exist_ok=True)
    start = (login_url or cfg.institution_login_url
             or "https://www.google.com/scholar")
    print(
        "\nOpening a browser. Log in through your institution as you normally "
        "would\n(including any MFA). The tool never sees your password — it only "
        "reuses\nthe session you create. Close the browser window when you're "
        "signed in.\n"
    )
    with sync_playwright() as pw:
        ctx = _launch_persistent(pw, cfg, headless=False)
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

    landing = _landing_url(cand, cfg)
    os.makedirs(cfg.out_dir, exist_ok=True)

    with sync_playwright() as pw:
        ctx = _launch_persistent(pw, cfg, headless=True, accept_downloads=True)
        try:
            page = ctx.new_page()
            page.goto(landing, wait_until="domcontentloaded",
                      timeout=int(cfg.timeout * 1000))
            time.sleep(cfg.min_request_interval)

            # A library resolver (SerialsSolutions/360 Link) shows an
            # intermediate "find full text" page rather than the article. Follow
            # its full-text link once to reach the publisher, then look for the
            # PDF there.
            pdf_href = _find_pdf_link(page)
            if not pdf_href:
                if _follow_fulltext_link(page, cfg):
                    time.sleep(cfg.min_request_interval)
                    pdf_href = _find_pdf_link(page)
            if not pdf_href:
                return None
            return _download_via_browser(ctx, page, pdf_href, cand, cfg)
        finally:
            _safe_close(ctx)


def _landing_url(cand: Candidate, cfg: Config) -> str:
    """Where to start the authenticated fetch for this paper."""
    # EZproxy: route the DOI through the proxy so the publisher page loads inside
    # the authenticated session. The proxy rewrites in-page links (including the
    # PDF link) to stay within the session, so no resolver hop is needed.
    if cfg.ezproxy_login_prefix and cand.doi:
        return cfg.ezproxy_login_prefix + publisher_landing_url(cand.doi)
    if cfg.resolver_openurl_base and cand.doi:
        from urllib.parse import quote
        return cfg.resolver_openurl_base + quote(cand.doi, safe="")
    if cand.doi:
        return publisher_landing_url(cand.doi)
    return cand.pdf_url


def _follow_fulltext_link(page, cfg: Config) -> bool:
    """On a resolver page, click the first 'full text / article' link.

    Returns True if it navigated somewhere new (i.e. on to the publisher).
    """
    before = page.url
    selectors = (
        'a:has-text("Full Text")', 'a:has-text("Full text")',
        'a:has-text("View Article")', 'a:has-text("Article")',
        'a:has-text("Download PDF")', 'a:has-text("PDF")',
        'a[href*="doi.org"]',
    )
    for sel in selectors:
        el = page.query_selector(sel)
        if not el:
            continue
        try:
            el.click()
            page.wait_for_load_state("domcontentloaded",
                                     timeout=int(cfg.timeout * 1000))
        except Exception:
            continue
        if page.url != before:
            return True
    return False


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
