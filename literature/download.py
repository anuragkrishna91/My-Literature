"""Download resolved candidates to disk, verifying they are actually PDFs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import List, Optional

from .config import Config
from .http import PoliteSession
from .resolve import Candidate


class DownloadResult:
    def __init__(self, query: str, path: Optional[str], source: Optional[str],
                 status: str, detail: str = ""):
        self.query = query
        self.path = path
        self.source = source
        self.status = status      # "downloaded" | "paywalled" | "not_found" | "error"
        self.detail = detail

    def as_dict(self) -> dict:
        return {
            "query": self.query, "path": self.path, "source": self.source,
            "status": self.status, "detail": self.detail,
        }


def download_first_available(query: str, candidates: List[Candidate],
                             session: PoliteSession, cfg: Config) -> DownloadResult:
    """Try each open-access candidate in order; return on the first real PDF."""
    if not candidates:
        return DownloadResult(query, None, None, "not_found",
                              "No open-access source found.")
    last_detail = ""
    for cand in candidates:
        try:
            path = _fetch_pdf(cand, session, cfg)
        except Exception as exc:  # noqa: BLE001 - report, try next candidate
            last_detail = f"{cand.source}: {exc}"
            continue
        if path:
            _record_metadata(cand, path, cfg)
            return DownloadResult(query, path, cand.source, "downloaded")
        last_detail = f"{cand.source}: response was not a PDF"
    return DownloadResult(query, None, None, "error",
                          last_detail or "All candidates failed.")


def _fetch_pdf(cand: Candidate, session: PoliteSession, cfg: Config) -> Optional[str]:
    resp = session.get(cand.pdf_url, accept="application/pdf", stream=True)
    if resp.status_code != 200:
        return None
    if not _is_pdf(resp):
        # Many paywalls return a 200 HTML login/abstract page instead of a PDF.
        return None
    os.makedirs(cfg.out_dir, exist_ok=True)
    filename = _filename_for(cand)
    path = os.path.join(cfg.out_dir, filename)
    with open(path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                fh.write(chunk)
    # A stub HTML page can still slip through; verify the magic bytes on disk.
    if not _file_is_pdf(path):
        os.remove(path)
        return None
    return path


def _is_pdf(resp) -> bool:
    ctype = resp.headers.get("Content-Type", "").lower()
    return "application/pdf" in ctype or "octet-stream" in ctype


def _file_is_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _filename_for(cand: Candidate) -> str:
    base = cand.doi or cand.title or "paper"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    return f"{safe[:120]}.pdf"


def _record_metadata(cand: Candidate, path: str, cfg: Config) -> None:
    index = os.path.join(cfg.out_dir, "metadata.jsonl")
    entry = {**asdict(cand), "path": os.path.abspath(path)}
    with open(index, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
