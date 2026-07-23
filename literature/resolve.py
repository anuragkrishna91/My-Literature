"""Resolve a DOI or title to the best *legal* source for the full text.

Order of preference, most-open first:
  1. Unpaywall  — aggregates open-access copies for a DOI.
  2. arXiv      — preprint full text (often identical to the published version).
  3. PubMed Central — open-access life-sciences full text.
Only if all of these miss is a paper considered "paywalled", and handed off to
the authenticated path (if the user has enabled it).

Title lookups are resolved to a DOI via Crossref first, then run through the
same pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .http import PoliteSession


@dataclass
class Candidate:
    """A resolved source for a paper's full text."""
    pdf_url: str
    source: str          # e.g. "unpaywall", "arxiv", "pmc"
    is_open_access: bool
    doi: Optional[str] = None
    title: Optional[str] = None


DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def looks_like_doi(text: str) -> bool:
    return bool(DOI_RE.match(text.strip()))


def resolve(query: str, session: PoliteSession, email: str) -> List[Candidate]:
    """Return open-access candidates for a DOI or title, best first."""
    query = query.strip()
    doi = query if looks_like_doi(query) else doi_from_title(query, session, email)
    if not doi:
        return []

    candidates: List[Candidate] = []
    for finder in (_unpaywall, _arxiv, _pmc):
        try:
            candidates.extend(finder(doi, session, email))
        except Exception:
            # A single source failing must never abort resolution.
            continue
    return candidates


def doi_from_title(title: str, session: PoliteSession, email: str) -> Optional[str]:
    """Best-effort DOI lookup for a title via Crossref."""
    url = (
        "https://api.crossref.org/works"
        f"?query.bibliographic={requests_quote(title)}&rows=1&mailto={email}"
    )
    resp = session.get(url, accept="application/json")
    if resp.status_code != 200:
        return None
    items = resp.json().get("message", {}).get("items", [])
    if not items:
        return None
    top = items[0]
    # Guard against a loose match: require meaningful title overlap.
    found_title = " ".join(top.get("title", []) or []).lower()
    if found_title and _title_similarity(title.lower(), found_title) < 0.6:
        return None
    return top.get("DOI")


def _unpaywall(doi: str, session: PoliteSession, email: str) -> List[Candidate]:
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    resp = session.get(url, accept="application/json")
    if resp.status_code != 200:
        return []
    data = resp.json()
    title = data.get("title")
    out: List[Candidate] = []
    best = data.get("best_oa_location") or {}
    pdf = best.get("url_for_pdf") or best.get("url")
    if pdf:
        out.append(Candidate(pdf, "unpaywall", True, doi=doi, title=title))
    for loc in data.get("oa_locations", []) or []:
        pdf = loc.get("url_for_pdf")
        if pdf and all(pdf != c.pdf_url for c in out):
            out.append(Candidate(pdf, "unpaywall", True, doi=doi, title=title))
    return out


def _arxiv(doi: str, session: PoliteSession, email: str) -> List[Candidate]:
    # arXiv exposes a DOI search via its Atom API.
    url = f"http://export.arxiv.org/api/query?search_query=doi:{doi}&max_results=1"
    resp = session.get(url)
    if resp.status_code != 200:
        return []
    m = re.search(r'<id>(http://arxiv\.org/abs/([^<]+))</id>', resp.text)
    if not m:
        return []
    arxiv_id = m.group(2).strip()
    pdf = f"https://arxiv.org/pdf/{arxiv_id}"
    return [Candidate(pdf, "arxiv", True, doi=doi)]


def _pmc(doi: str, session: PoliteSession, email: str) -> List[Candidate]:
    # NCBI's ID converter maps a DOI to a PMC id when an open-access copy exists.
    url = (
        "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        f"?tool=my-literature&email={email}&ids={doi}&format=json"
    )
    resp = session.get(url, accept="application/json")
    if resp.status_code != 200:
        return []
    records = resp.json().get("records", [])
    if not records or "pmcid" not in records[0]:
        return []
    pmcid = records[0]["pmcid"]
    pdf = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
    return [Candidate(pdf, "pmc", True, doi=doi)]


def publisher_landing_url(doi: str) -> str:
    """The canonical DOI resolver URL — where the authenticated path starts."""
    return f"https://doi.org/{doi}"


# --- small helpers -----------------------------------------------------------

def requests_quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


def _title_similarity(a: str, b: str) -> float:
    """Cheap token Jaccard similarity, enough to reject a wrong Crossref hit."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
