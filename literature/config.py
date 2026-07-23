"""Runtime configuration, sourced from environment and CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


DEFAULT_USER_AGENT = (
    "My-Literature/0.1 (personal reference manager; "
    "polite, rate-limited; contact: {email})"
)


@dataclass
class Config:
    # Contact email — required by Unpaywall and encouraged by Crossref etiquette.
    # A tool that identifies itself is treated far better than an anonymous one.
    email: str

    # Directory downloads are written to.
    out_dir: str = "library"

    # Minimum seconds between any two outbound requests to the same host.
    # Deliberately conservative — this is what keeps you off publisher radar.
    min_request_interval: float = 3.0

    # Hard ceiling on downloads in a single run. A safety rail against an
    # accidental loop turning into a bulk-scrape that trips fraud detection.
    max_per_run: int = 50

    # HTTP timeout (seconds) and retry policy.
    timeout: float = 30.0
    max_retries: int = 3

    # Whether the authenticated browser-session fallback is permitted.
    allow_auth: bool = False

    # Where the persistent browser profile (cookies/session) is stored, so you
    # only log in occasionally. Contains session cookies — keep it private.
    browser_profile_dir: str = os.path.expanduser("~/.my-literature/browser-profile")

    # Institutional access (optional). When set, the authenticated path uses
    # these instead of going straight to the publisher via doi.org.
    #   institution_login_url : the page opened for you to log in (your library).
    #   resolver_openurl_base : a URL prefix that, with a DOI appended, resolves
    #                           to the full text through your library (e.g. a
    #                           SerialsSolutions/360 Link OpenURL endpoint).
    institution_login_url: Optional[str] = None
    resolver_openurl_base: Optional[str] = None

    @property
    def user_agent(self) -> str:
        return DEFAULT_USER_AGENT.format(email=self.email)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        email = overrides.pop("email", None) or os.environ.get("LITERATURE_EMAIL", "")
        if not email:
            raise SystemExit(
                "A contact email is required. Set LITERATURE_EMAIL or pass --email.\n"
                "Unpaywall and Crossref ask for one so they can reach you instead of "
                "silently blocking the tool; supplying it is basic good etiquette."
            )
        cfg = cls(email=email)
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg
