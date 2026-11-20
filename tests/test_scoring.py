"""Scoring-maths tests for seo-geo-audit-cli.

Every number asserted here is either computed by the code under test or
read from the real sample reports the CLI itself generated
(sample_output/report*.md) — nothing is invented.
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from seo_audit.checks import CHECKS, audit, grade
from seo_audit.crawler import CrawlResult, Page

ROOT = Path(__file__).resolve().parent.parent

# --- expected numbers from the real sample reports -----------------------
EXPECTED = {
    "report.md": 68,          # flawed test site
    "report-fixed.md": 89,    # same site, flaws fixed
    "report-toolenix.md": 85,  # real site: toolenix.com, 20 pages
}
TOOLENIX_CATEGORIES = {
    "Technical": 99,
    "On-page SEO": 94,
    "Links": 71,
    "GEO": 65,
}


def _parse_report(path: Path) -> tuple[int, dict]:
    text = path.read_text(encoding="utf-8")
    total = int(re.search(r"\*\*Score: (\d+)/100", text).group(1))
    cats = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"\| (Technical|On-page SEO|Links|GEO) \| (\d+)/100 \|", text)
    }
    return total, cats


def test_grade_boundaries():
    assert grade(95) == "A — excellent"
    assert grade(90) == "A — excellent"
    assert grade(89) == "B — good"
    assert grade(85) == "B — good"
    assert grade(68) == "C — needs work"
    assert grade(60) == "C — needs work"
    assert grade(40) == "D — poor"
    assert grade(20) == "F — critical"


def test_sample_report_totals_are_the_real_cli_numbers():
    """The totals quoted in the README are the numbers the CLI produced."""
    for name, expected in EXPECTED.items():
        total, _ = _parse_report(ROOT / "sample_output" / name)
        assert total == expected, f"{name}: got {total}, expected {expected}"


def test_toolenix_category_scores():
    """toolenix.com 85/100 breaks down as Technical 99, On-page 94,
    Links 71, GEO 65 — the weakest area is GEO, which is the point."""
    _, cats = _parse_report(ROOT / "sample_output" / "report-toolenix.md")
    assert cats == TOOLENIX_CATEGORIES


def test_fixed_site_beats_flawed_site_by_21_points():
    before, _ = _parse_report(ROOT / "sample_output" / "report.md")
    after, _ = _parse_report(ROOT / "sample_output" / "report-fixed.md")
    assert after - before == 21


# --- synthetic crawls: the weighted-total formula itself -----------------
def _perfect_page() -> Page:
    body_words = (
        "Tomatoes need sun, water and good soil to grow well. "
        "They grow best in soil with a pH between 6.0 and 6.8, "
        "take 60 to 80 days to ripen, and need 2 inches of water a week. "
    ) * 30  # ~900 words, figures included
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Complete Guide to Growing Tomatoes at Home</title>
<meta name="description" content="Learn how to grow tomatoes at home: soil, watering, feeding and harvest. A practical guide with steps and tips.">
<link rel="canonical" href="https://example.com/">
<meta property="og:title" content="The Complete Guide to Growing Tomatoes at Home">
<meta property="og:description" content="Learn how to grow tomatoes at home: soil, watering, feeding and harvest.">
<meta property="og:image" content="https://example.com/tomato.jpg">
<script type="application/ld+json">{{"@type": "FAQPage", "mainEntity": []}}</script>
<script type="application/ld+json">{{"@type": "HowTo", "step": []}}</script>
</head><body>
<h1>How to Grow Tomatoes at Home</h1>
<h2>How much water do tomatoes need?</h2>
<time datetime="2026-11-20">20 November 2026</time>
<p>{body_words}</p>
<ul><li>Sun</li><li>Water</li></ul>
<img src="/tomato.jpg" alt="Ripe tomatoes on the vine">
<a href="https://example.com/about">About</a>
</body></html>"""
    return Page(
        url="https://example.com/",
        status=200,
        final_url="https://example.com/",
        size_bytes=5000,
        elapsed_s=0.1,
        html=html,
        soup=BeautifulSoup(html, "html.parser"),
        links_internal=["https://example.com/about"],
    )


def _perfect_crawl() -> CrawlResult:
    page = _perfect_page()
    return CrawlResult(
        start_url="https://example.com/",
        pages=[page],
        failed_pages=[],
        external_status={},
        robots_txt_ok=True,
        sitemap_ok=True,
        sitemap_urls=["https://example.com/"],
        llms_txt_ok=True,
        llms_txt_text="Example\n\n## Guides\n\n- [Home](https://example.com/)\n",
    )


def test_perfect_site_scores_100_in_every_category():
    result = audit(_perfect_crawl())
    assert result.total == 100
    assert all(pct == 100 for _, _, pct in result.categories.values())
    assert result.fixes == []


def test_broken_internal_link_costs_eight_points_and_tops_the_fix_list():
    crawl = _perfect_crawl()
    crawl.failed_pages = [
        {"url": "https://example.com/missing", "error": "HTTP 404"}
    ]
    crawl.llms_txt_ok = False  # second, smaller fault (weight 5)
    result = audit(crawl)
    total_avail = sum(c.weight for c in CHECKS)
    assert result.total == round(100 * (total_avail - 8 - 5) / total_avail)
    assert result.fixes[0]["check"] == "broken_internal"
    assert result.fixes[0]["impact"] == 8.0
    assert result.fixes[1]["check"] == "llms_txt"
    assert result.fixes[1]["impact"] == 5.0
    # Fixes are sorted highest-impact first.
    impacts = [f["impact"] for f in result.fixes]
    assert impacts == sorted(impacts, reverse=True)
