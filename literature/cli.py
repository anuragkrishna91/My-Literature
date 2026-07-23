"""Command-line interface for My-Literature."""

from __future__ import annotations

import argparse
import sys
from typing import List

from .config import Config
from .http import PoliteSession
from .pipeline import fetch_one


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--email", help="Contact email (or set LITERATURE_EMAIL).")
    p.add_argument("--out", dest="out_dir", help="Output directory (default: library).")
    p.add_argument("--allow-auth", action="store_true",
                   help="Permit the authenticated browser-session fallback for "
                        "paywalled papers.")
    p.add_argument("--rate", dest="min_request_interval", type=float,
                   help="Minimum seconds between requests to a host (default: 3).")
    p.add_argument("--max", dest="max_per_run", type=int,
                   help="Max downloads per run (default: 50).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="literature",
        description="Polite, resolver-first academic paper downloader.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    get = sub.add_parser("get", help="Download a single paper.")
    get.add_argument("query", nargs="?", help="A DOI (or use --title).")
    get.add_argument("--title", help="Resolve and download by title.")
    _add_common(get)

    batch = sub.add_parser("batch", help="Download from a file of DOIs/titles.")
    batch.add_argument("file", help="One DOI or title per line ('#' comments ok).")
    _add_common(batch)

    login = sub.add_parser(
        "login", help="Open a browser to establish/refresh your institutional session.")
    _add_common(login)
    return parser


def _config_from_args(args) -> Config:
    return Config.from_env(
        email=getattr(args, "email", None),
        out_dir=getattr(args, "out_dir", None),
        allow_auth=getattr(args, "allow_auth", False) or None,
        min_request_interval=getattr(args, "min_request_interval", None),
        max_per_run=getattr(args, "max_per_run", None),
    )


def _read_queries(path: str) -> List[str]:
    out: List[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _config_from_args(args)

    if args.command == "login":
        from .auth import ensure_logged_in
        ensure_logged_in(cfg)
        print("Session saved. Future --allow-auth downloads will reuse it.")
        return 0

    session = PoliteSession(cfg.user_agent, cfg.min_request_interval,
                            cfg.timeout, cfg.max_retries)
    try:
        if args.command == "get":
            query = args.title or args.query
            if not query:
                print("Provide a DOI argument or --title.", file=sys.stderr)
                return 2
            queries = [query]
        else:  # batch
            queries = _read_queries(args.file)

        if len(queries) > cfg.max_per_run:
            print(f"Refusing to fetch {len(queries)} papers in one run "
                  f"(cap is {cfg.max_per_run}). Split the list or raise --max "
                  f"deliberately.", file=sys.stderr)
            return 2

        downloaded = 0
        for query in queries:
            result = fetch_one(query, session, cfg)
            _report(result)
            if result.status == "downloaded":
                downloaded += 1
        print(f"\nDone: {downloaded}/{len(queries)} downloaded into {cfg.out_dir}/")
        return 0 if downloaded else 1
    finally:
        session.close()


def _report(result) -> None:
    marks = {"downloaded": "[ok]", "paywalled": "[paywalled]",
             "not_found": "[not found]", "error": "[error]"}
    mark = marks.get(result.status, "[?]")
    line = f"{mark} {result.query}"
    if result.source:
        line += f"  ({result.source})"
    print(line)
    if result.detail:
        print(f"      {result.detail}")


if __name__ == "__main__":
    raise SystemExit(main())
