"""Classify a paper's publisher from its DOI prefix (with a journal-name fallback).

The DOI registrant prefix (the 10.xxxx part) maps cleanly to a publisher for the
large majority of papers, and needs no extra API call. Where the prefix is
unknown, we fall back to keyword hints in the venue/journal name.
"""

from __future__ import annotations

from typing import Optional

# DOI prefix -> publisher label. Covers the common chemistry/materials/energy
# and general-science publishers; extend freely.
DOI_PREFIX_PUBLISHER = {
    "10.1038": "Nature (Springer Nature)",
    "10.1007": "Springer",
    "10.1186": "BMC (Springer Nature)",
    "10.1039": "RSC",
    "10.1021": "ACS",
    "10.1002": "Wiley",
    "10.1111": "Wiley",
    "10.1016": "Elsevier",
    "10.1015": "Elsevier",
    "10.3390": "MDPI",
    "10.1109": "IEEE",
    "10.1088": "IOP",
    "10.1063": "AIP",
    "10.1103": "APS",
    "10.1080": "Taylor & Francis",
    "10.1126": "Science (AAAS)",
    "10.1073": "PNAS",
    "10.1371": "PLOS",
    "10.1155": "Hindawi",
    "10.1093": "Oxford University Press",
    "10.1364": "Optica (OSA)",
    "10.1149": "Electrochemical Society",
    "10.1246": "Chemical Society of Japan",
    "10.1021/acs": "ACS",
    "10.1042": "Portland Press",
    "10.1098": "Royal Society",
    "10.1101": "Cold Spring Harbor / bioRxiv",
    "10.48550": "arXiv",
    "10.26434": "ChemRxiv",
    "10.1201": "Taylor & Francis (CRC)",
    "10.4028": "Trans Tech (Scientific.Net)",
    "10.1177": "SAGE",
    "10.1145": "ACM",
    "10.1594": "Copernicus",
    "10.5194": "Copernicus",
}

# Fallback: substrings in the journal/venue name -> publisher family.
JOURNAL_HINTS = (
    ("nature", "Nature (Springer Nature)"),
    ("acs ", "ACS"),
    ("journal of the american chemical society", "ACS"),
    ("chemistry of materials", "ACS"),
    ("rsc ", "RSC"),
    ("chemical science", "RSC"),
    ("energy & environmental science", "RSC"),
    ("journal of materials chemistry", "RSC"),
    ("advanced ", "Wiley"),          # Advanced Materials/Energy Materials, etc.
    ("angewandte", "Wiley"),
    ("small", "Wiley"),
    ("elsevier", "Elsevier"),
    ("ieee", "IEEE"),
    ("acm", "ACM"),
    ("mdpi", "MDPI"),
    ("plos", "PLOS"),
    ("science", "Science (AAAS)"),
)


def publisher_for_doi(doi: Optional[str]) -> Optional[str]:
    """Return a publisher label from a DOI's registrant prefix, or None."""
    if not doi:
        return None
    doi = doi.strip().replace("https://doi.org/", "").lower()
    # Try a longer 'prefix/first-segment' key first (e.g. 10.1021/acs), then the
    # bare 10.xxxx prefix.
    parts = doi.split("/", 1)
    prefix = parts[0]
    if len(parts) > 1:
        long_key = f"{prefix}/{parts[1].split('.')[0]}"
        if long_key in DOI_PREFIX_PUBLISHER:
            return DOI_PREFIX_PUBLISHER[long_key]
    return DOI_PREFIX_PUBLISHER.get(prefix)


def publisher_for_record(rec: dict) -> str:
    """Best-effort publisher label for a search record ({doi, journal, ...})."""
    by_doi = publisher_for_doi(rec.get("doi"))
    if by_doi:
        return by_doi
    journal = (rec.get("journal") or "").lower()
    for needle, label in JOURNAL_HINTS:
        if needle in journal:
            return label
    return "Other"
