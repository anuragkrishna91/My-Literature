"""Offline tests for resolution and download logic (no network required).

We stub the HTTP layer so the resolve -> download pipeline can be verified
without reaching external APIs.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from literature.config import Config
from literature.resolve import resolve, looks_like_doi
from literature.download import download_first_available, _file_is_pdf


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class FakeResponse:
    def __init__(self, status=200, json_data=None, content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self._content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace") if content else ""

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1):
        yield self._content


class FakeSession:
    """Routes URLs to canned responses by substring match."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, accept=None, allow_redirects=True, stream=False):
        self.calls.append(url)
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        return FakeResponse(status=404)


def test_looks_like_doi():
    assert looks_like_doi("10.1371/journal.pone.0173664")
    assert not looks_like_doi("Attention is all you need")


def test_resolve_prefers_unpaywall_open_access():
    routes = {
        "api.unpaywall.org": FakeResponse(json_data={
            "title": "A Test Paper",
            "best_oa_location": {"url_for_pdf": "https://oa.example/paper.pdf"},
            "oa_locations": [],
        }),
    }
    session = FakeSession(routes)
    cands = resolve("10.1234/abcd", session, "e@x.edu")
    assert cands, "expected at least one candidate"
    assert cands[0].source == "unpaywall"
    assert cands[0].pdf_url == "https://oa.example/paper.pdf"
    assert cands[0].is_open_access


def test_download_rejects_non_pdf_html_login_page():
    cfg = Config(email="e@x.edu", out_dir="/tmp/mylit-test-out")
    routes = {
        "login.example/paper.pdf": FakeResponse(
            content=b"<html>Please sign in</html>",
            headers={"Content-Type": "text/html"},
        ),
    }
    session = FakeSession(routes)
    from literature.resolve import Candidate
    cand = Candidate("https://login.example/paper.pdf", "unpaywall", True, doi="10.1/x")
    result = download_first_available("10.1/x", [cand], session, cfg)
    assert result.status == "error"
    assert result.path is None


def test_download_writes_real_pdf():
    out = "/tmp/mylit-test-out2"
    cfg = Config(email="e@x.edu", out_dir=out)
    routes = {
        "oa.example/paper.pdf": FakeResponse(
            content=MINIMAL_PDF,
            headers={"Content-Type": "application/pdf"},
        ),
    }
    session = FakeSession(routes)
    from literature.resolve import Candidate
    cand = Candidate("https://oa.example/paper.pdf", "unpaywall", True, doi="10.1/y")
    result = download_first_available("10.1/y", [cand], session, cfg)
    assert result.status == "downloaded", result.detail
    assert _file_is_pdf(result.path)
    # metadata index should have one line
    with open(os.path.join(out, "metadata.jsonl")) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert rows[-1]["source"] == "unpaywall"


def test_not_found_when_no_candidates():
    cfg = Config(email="e@x.edu", out_dir="/tmp/mylit-test-out3")
    session = FakeSession({})
    result = download_first_available("10.1/z", [], session, cfg)
    assert result.status == "not_found"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
