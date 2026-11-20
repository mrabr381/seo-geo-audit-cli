"""CLI: python -m seo_audit --url https://example.com --out report/

Exit codes: 0 = audit completed (whatever the score), 1 = the audit itself
failed (bad URL, start page unreachable), 2 = usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from seo_audit.checks import audit, grade
from seo_audit.crawler import Crawler
from seo_audit.report import write_all


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seo_audit",
        description="Free SEO + GEO site audit — crawl a site, score it, write a report.")
    p.add_argument("--url", required=True, help="Site to audit, e.g. https://example.com")
    p.add_argument("--max-pages", type=int, default=50,
                   help="Maximum pages to crawl (default 50)")
    p.add_argument("--max-depth", type=int, default=3,
                   help="Maximum link depth from the start page (default 3)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="Per-request timeout in seconds (default 10)")
    p.add_argument("--no-external", action="store_true",
                   help="Skip checking external links (faster, offline-friendly)")
    p.add_argument("--out", default="report/", help="Output directory (default report/)")
    return p


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_pages < 1:
        print("Error: --max-pages must be at least 1.", file=sys.stderr)
        return 2
    if args.max_depth < 0:
        print("Error: --max-depth must be at least 0.", file=sys.stderr)
        return 2

    try:
        crawler = Crawler(args.url, max_pages=args.max_pages,
                          max_depth=args.max_depth, timeout=args.timeout,
                          check_external=not args.no_external)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Auditing {crawler.start_url} (max {args.max_pages} pages, depth {args.max_depth}) …")
    try:
        crawl = crawler.crawl()
    except Exception as exc:  # network-level failure of the whole crawl
        print(f"Error: audit failed — {exc}", file=sys.stderr)
        return 1
    if not crawl.pages and not crawl.failed_pages:
        print(f"Error: could not fetch {crawler.start_url} at all.", file=sys.stderr)
        return 1
    if not crawl.pages:
        print(f"Error: start page unreachable: "
              f"{crawl.failed_pages[0]['error'] if crawl.failed_pages else 'unknown'}",
              file=sys.stderr)
        return 1

    result = audit(crawl)
    out_dir = Path(args.out)
    written = write_all(result, out_dir)

    print(f"\nScore: {result.total}/100 — {grade(result.total)}")
    for cat, (_, _, pct) in result.categories.items():
        print(f"  {cat}: {pct}/100")
    print(f"Pages audited: {result.pages_audited} · failed to load: {result.pages_failed}")
    print("\nTop fixes:")
    for fix in result.fixes[:5]:
        print(f"  - {fix['title']} ({fix['category']}, impact {fix['impact']}): {fix['fix']}")
    print("\nReports written:")
    for name, path in written.items():
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
