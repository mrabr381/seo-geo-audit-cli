"""The audit checks: ~26 weighted checks in 4 categories.

Every score is computed from the real crawl — nothing is hardcoded.
A check returns a score from 0.0 to 1.0 (or None for "not applicable",
which excludes it from the denominator), plus a human-readable detail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from seo_audit.crawler import CrawlResult, Page

QUESTION_WORDS = ("what", "why", "how", "when", "where", "which", "who",
                  "can", "should", "is", "are", "does", "do")
DATE_RE = re.compile(
    r"\b(19|20)\d{2}[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])\b"
    r"|\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+(19|20)\d{2}\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- helpers
def _text(page: Page) -> str:
    # Non-mutating: the old version decomposed <script> tags, which silently
    # destroyed the JSON-LD before the json_ld check ran later in the list.
    parts = []
    for element in page.soup.find_all(string=True):
        if element.parent.name in ("script", "style", "noscript"):
            continue
        text = element.strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _words(page: Page) -> int:
    return len(_text(page).split())


def _title(page: Page) -> str:
    tag = page.soup.title
    return tag.get_text(strip=True) if tag else ""


def _meta(page: Page, name: str) -> str:
    tag = page.soup.find("meta", attrs={"name": name}) or \
        page.soup.find("meta", attrs={"property": name})
    return (tag.get("content") or "").strip() if tag else ""


def _headings(page: Page) -> list:
    return [(int(t.name[1]), t.get_text(strip=True))
            for t in page.soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]


def _json_ld_types(page: Page) -> set:
    types = set()
    for tag in page.soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                t = item.get("@type")
                if isinstance(t, str):
                    types.add(t)
                elif isinstance(t, list):
                    types.update(x for x in t if isinstance(x, str))
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for node in graph:
                        if isinstance(node, dict) and isinstance(node.get("@type"), str):
                            types.add(node["@type"])
    return types


def _imgs(page: Page) -> list:
    return page.soup.find_all("img")


# ---------------------------------------------------------------- checks
@dataclass
class Check:
    id: str
    category: str
    weight: int
    title: str
    fix: str
    kind: str  # "site" or "page"


@dataclass
class CheckResult:
    check: Check
    score: float | None  # None = not applicable
    detail: str
    failing_pages: list = field(default_factory=list)


def _site_https(crawl: CrawlResult):
    ok = urlparse(crawl.start_url).scheme == "https"
    return (1.0 if ok else 0.0,
            "Site served over HTTPS" if ok else "Site is plain HTTP — browsers flag it as not secure")


def _site_robots(crawl: CrawlResult):
    return (1.0 if crawl.robots_txt_ok else 0.0,
            "robots.txt found" if crawl.robots_txt_ok else "No robots.txt at /robots.txt")


def _site_sitemap(crawl: CrawlResult):
    if not crawl.sitemap_ok:
        return 0.0, "No sitemap.xml at /sitemap.xml"
    n = len(crawl.sitemap_urls)
    return 1.0, f"sitemap.xml found with {n} URL(s)"


def _site_sitemap_urls(crawl: CrawlResult):
    if not crawl.sitemap_ok or not crawl.sitemap_urls:
        return None, "no sitemap to validate"
    crawled = {p.final_url for p in crawl.pages} | {p.url for p in crawl.pages}
    bad = [u for u in crawl.sitemap_urls if u not in crawled]
    # A sitemap URL is only "bad" if we actually fetched the site and it 404s;
    # unvisited URLs (beyond max-pages) are not evidence of breakage.
    broken = [u for u in bad if any(f["url"] == u for f in crawl.failed_pages)]
    if broken:
        return 0.0, f"{len(broken)} sitemap URL(s) return errors: {', '.join(broken[:3])}"
    return 1.0, f"all checked sitemap URLs resolve ({len(crawl.sitemap_urls)} listed)"


def _site_llms(crawl: CrawlResult):
    if not crawl.llms_txt_ok:
        return 0.0, "No llms.txt — AI search engines have no machine-readable map of the site"
    lines = [ln for ln in crawl.llms_txt_text.splitlines() if ln.strip()]
    return 1.0, f"llms.txt found ({len(lines)} non-empty lines)"


def _site_broken_internal(crawl: CrawlResult):
    broken = [f["url"] for f in crawl.failed_pages
              if not f["error"].startswith("blocked by robots")]
    if not broken:
        return 1.0, "no broken internal links found"
    return 0.0, f"{len(broken)} broken internal link(s): {', '.join(broken[:3])}"


def _site_broken_external(crawl: CrawlResult):
    broken = {u: v for u, v in crawl.external_status.items()
              if v[0] is None or v[0] >= 400}
    if not broken:
        return 1.0, f"all {len(crawl.external_status)} external link(s) resolve"
    shown = ", ".join(f"{u} ({v[1]})" for u, v in list(broken.items())[:3])
    return 0.0, f"{len(broken)} broken external link(s): {shown}"


def _site_titles_unique(crawl: CrawlResult):
    titles = [_title(p) for p in crawl.pages if _title(p)]
    dupes = len(titles) - len(set(titles))
    if dupes == 0:
        return 1.0, f"all {len(titles)} page title(s) unique"
    return 0.0, f"{dupes} duplicate title(s) across {len(titles)} pages"


def _site_faq_schema(crawl: CrawlResult):
    for p in crawl.pages:
        if "FAQPage" in _json_ld_types(p):
            return 1.0, f"FAQPage schema found ({p.url})"
    return 0.0, "No FAQPage schema anywhere — AI answers can't lift ready-made Q&As"


def _site_howto_schema(crawl: CrawlResult):
    for p in crawl.pages:
        if "HowTo" in _json_ld_types(p):
            return 1.0, f"HowTo schema found ({p.url})"
    return 0.0, "No HowTo schema anywhere — step-by-step content is not machine-readable"


def _page_title_present(page: Page):
    t = _title(page)
    return (1.0 if t else 0.0), (f"title: {t[:60]}" if t else "no <title> tag")


def _page_title_length(page: Page):
    t = _title(page)
    if not t:
        return None, "no title to measure"
    n = len(t)
    if 30 <= n <= 60:
        return 1.0, f"title {n} chars (ideal 30–60)"
    if 15 <= n <= 70:
        return 0.5, f"title {n} chars (ideal 30–60)"
    return 0.0, f"title {n} chars (ideal 30–60)"


def _page_meta_desc(page: Page):
    d = _meta(page, "description")
    return (1.0 if d else 0.0), ("meta description present" if d else "no meta description")


def _page_meta_desc_length(page: Page):
    d = _meta(page, "description")
    if not d:
        return None, "no meta description to measure"
    n = len(d)
    if 50 <= n <= 160:
        return 1.0, f"meta description {n} chars (ideal 50–160)"
    if 25 <= n <= 200:
        return 0.5, f"meta description {n} chars (ideal 50–160)"
    return 0.0, f"meta description {n} chars (ideal 50–160)"


def _page_h1_present(page: Page):
    n = len(page.soup.find_all("h1"))
    return (1.0 if n >= 1 else 0.0), (f"{n} H1(s)" if n else "no H1")


def _page_h1_single(page: Page):
    n = len(page.soup.find_all("h1"))
    if n == 0:
        return None, "no H1 to judge"
    return (1.0 if n == 1 else 0.0), (f"exactly one H1" if n == 1 else f"{n} H1s — should be one")


def _page_heading_hierarchy(page: Page):
    levels = [lvl for lvl, _ in _headings(page)]
    if len(levels) <= 1:
        return 1.0, "too few headings to judge — treated as pass" if levels else "no headings"
    prev = levels[0]
    for lvl in levels[1:]:
        if lvl > prev + 1:
            return 0.0, f"heading level jumps H{prev} → H{lvl}"
        prev = lvl
    return 1.0, "heading levels step down cleanly"


def _page_canonical(page: Page):
    tag = page.soup.find("link", attrs={"rel": "canonical"})
    return (1.0 if tag and tag.get("href") else 0.0), \
        ("canonical present" if tag and tag.get("href") else "no canonical link")


def _page_og(page: Page):
    ok = bool(_meta(page, "og:title")) and bool(_meta(page, "og:description"))
    return (1.0 if ok else 0.0), ("og:title + og:description present" if ok else "missing og:title or og:description")


def _page_og_image(page: Page):
    ok = bool(_meta(page, "og:image"))
    return (1.0 if ok else 0.0), ("og:image present" if ok else "no og:image")


def _page_viewport(page: Page):
    tag = page.soup.find("meta", attrs={"name": "viewport"})
    return (1.0 if tag else 0.0), ("viewport meta present" if tag else "no viewport meta — mobile layout untested")


def _page_charset(page: Page):
    ok = page.soup.find("meta", attrs={"charset": True}) is not None
    return (1.0 if ok else 0.0), ("charset declared" if ok else "no charset meta")


def _page_lang(page: Page):
    html = page.soup.find("html")
    ok = html is not None and bool(html.get("lang"))
    return (1.0 if ok else 0.0), ("<html lang> present" if ok else "no lang attribute on <html>")


def _page_alt(page: Page):
    imgs = _imgs(page)
    if not imgs:
        return None, "no images on page"
    with_alt = sum(1 for i in imgs if (i.get("alt") or "").strip())
    frac = with_alt / len(imgs)
    return frac, f"{with_alt}/{len(imgs)} images have alt text"


def _page_words(page: Page):
    n = _words(page)
    if n >= 300:
        return 1.0, f"{n} words"
    if n >= 150:
        return 0.5, f"only {n} words (300+ is a solid page)"
    return 0.0, f"only {n} words — thin content"


def _page_internal_links(page: Page):
    n = len(page.links_internal)
    return (1.0 if n >= 1 else 0.0), (f"{n} internal link(s)" if n else "no internal links")


def _page_json_ld(page: Page):
    types = _json_ld_types(page)
    return (1.0 if types else 0.0), \
        (f"structured data: {', '.join(sorted(types))}" if types else "no JSON-LD structured data")


def _page_questions(page: Page):
    heads = [text.lower() for lvl, text in _headings(page) if lvl in (2, 3)]
    # A heading is a question if it ends with "?", or starts with a question
    # word — except statement-style headings like "What we believe".
    statements = ("what we ", "what our ", "why we ", "why our ")
    q = [h for h in heads
         if h.endswith("?")
         or (h.split(" ")[0] in QUESTION_WORDS
             and not h.startswith(statements))]
    return (1.0 if q else 0.0), (f"{len(q)} question heading(s)" if q else "no question-style H2/H3 headings")


def _page_dates(page: Page):
    if page.soup.find("time"):
        return 1.0, "<time> element present"
    if DATE_RE.search(_text(page)):
        return 1.0, "a written date is present"
    return 0.0, "no date found — AI answers prefer dated, verifiable facts"


def _page_facts(page: Page):
    text = _text(page)
    numbers = len(re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|kg|km|cm|mm|years?|months?|days?|£|\$|€)?\b", text))
    return (1.0 if numbers >= 3 else 0.0), (f"{numbers} figure(s) in the copy" if numbers else "no figures — add numbers, dates or stats")


def _page_lists_tables(page: Page):
    n = len(page.soup.find_all(["ul", "ol", "table"]))
    return (1.0 if n >= 1 else 0.0), (f"{n} list(s)/table(s) — citation-friendly structure" if n else "no lists or tables")


def _page_size(page: Page):
    kb = page.size_bytes / 1024
    if kb <= 200:
        return 1.0, f"page {kb:.0f} KB"
    if kb <= 500:
        return 0.5, f"page {kb:.0f} KB — heavy; 200 KB or less is fast"
    return 0.0, f"page {kb:.0f} KB — very heavy for mobile"


def _page_resources(page: Page):
    scripts = len([s for s in page.soup.find_all("script", src=True)])
    styles = len([l for l in page.soup.find_all("link", rel="stylesheet")])
    n = scripts + styles
    if n <= 6:
        return 1.0, f"{n} render-blocking resource(s)"
    if n <= 12:
        return 0.5, f"{n} render-blocking resources"
    return 0.0, f"{n} render-blocking resources — slows first paint"


CHECKS = [
    Check("https", "Technical", 4, "HTTPS", "Serve the site over HTTPS.", "site"),
    Check("robots_txt", "Technical", 3, "robots.txt present", "Add a robots.txt at /robots.txt.", "site"),
    Check("sitemap", "Technical", 4, "sitemap.xml present", "Add a sitemap.xml listing every page.", "site"),
    Check("sitemap_urls", "Technical", 2, "Sitemap URLs resolve", "Remove dead URLs from sitemap.xml.", "site"),
    Check("llms_txt", "GEO", 5, "llms.txt present", "Add an llms.txt at /llms.txt listing key pages.", "site"),
    Check("broken_internal", "Links", 8, "No broken internal links", "Fix or remove the broken internal links.", "site"),
    Check("broken_external", "Links", 4, "No broken external links", "Fix or remove the broken external links.", "site"),
    Check("titles_unique", "On-page SEO", 4, "Unique titles", "Give every page a unique <title>.", "site"),
    Check("faq_schema", "GEO", 3, "FAQPage schema", "Add FAQPage JSON-LD to pages that answer questions.", "site"),
    Check("howto_schema", "GEO", 3, "HowTo schema", "Add HowTo JSON-LD to step-by-step pages.", "site"),
    Check("title_present", "On-page SEO", 4, "Title present", "Add a <title> to the page.", "page"),
    Check("title_length", "On-page SEO", 4, "Title length 30–60 chars", "Rewrite the title to 30–60 characters.", "page"),
    Check("meta_desc", "On-page SEO", 4, "Meta description present", "Add a meta description.", "page"),
    Check("meta_desc_length", "On-page SEO", 3, "Meta description 50–160 chars", "Rewrite the meta description to 50–160 characters.", "page"),
    Check("h1_present", "On-page SEO", 2, "H1 present", "Add one H1 stating the page topic.", "page"),
    Check("h1_single", "On-page SEO", 3, "Exactly one H1", "Keep exactly one H1 per page.", "page"),
    Check("heading_hierarchy", "On-page SEO", 3, "Heading hierarchy", "Don't skip heading levels (H1 → H3).", "page"),
    Check("canonical", "Technical", 3, "Canonical link", "Add a canonical link to the page.", "page"),
    Check("og_tags", "On-page SEO", 3, "Open Graph title + description", "Add og:title and og:description.", "page"),
    Check("og_image", "On-page SEO", 1, "Open Graph image", "Add an og:image.", "page"),
    Check("viewport", "Technical", 3, "Viewport meta", "Add <meta name=\"viewport\"> for mobile.", "page"),
    Check("charset", "Technical", 1, "Charset declared", "Add <meta charset>.", "page"),
    Check("lang", "Technical", 1, "HTML lang attribute", "Add lang=\"en\" (or the page language) to <html>.", "page"),
    Check("alt_coverage", "On-page SEO", 4, "Image alt text", "Write descriptive alt text for every image.", "page"),
    Check("content_length", "On-page SEO", 3, "Content length 300+ words", "Expand thin pages to 300+ words.", "page"),
    Check("internal_links", "Links", 2, "Internal links", "Link the page into the rest of the site.", "page"),
    Check("json_ld", "GEO", 3, "JSON-LD structured data", "Add JSON-LD structured data.", "page"),
    Check("questions", "GEO", 3, "Question headings", "Phrase H2/H3s as the questions people ask.", "page"),
    Check("dates", "GEO", 2, "Dates present", "Show a published/updated date.", "page"),
    Check("facts", "GEO", 2, "Figures and facts", "Add verifiable numbers, dates or stats.", "page"),
    Check("lists_tables", "GEO", 2, "Lists / tables", "Structure key facts as lists or tables.", "page"),
    Check("page_size", "Technical", 3, "Page size ≤ 200 KB", "Shrink the page — compress images, drop unused JS/CSS.", "page"),
    Check("resources", "Technical", 3, "Few render-blocking resources", "Cut render-blocking scripts and stylesheets.", "page"),
]

SITE_FUNCS = {
    "https": _site_https,
    "robots_txt": _site_robots,
    "sitemap": _site_sitemap,
    "sitemap_urls": _site_sitemap_urls,
    "llms_txt": _site_llms,
    "broken_internal": _site_broken_internal,
    "broken_external": _site_broken_external,
    "titles_unique": _site_titles_unique,
    "faq_schema": _site_faq_schema,
    "howto_schema": _site_howto_schema,
}

PAGE_FUNCS = {
    "title_present": _page_title_present,
    "title_length": _page_title_length,
    "meta_desc": _page_meta_desc,
    "meta_desc_length": _page_meta_desc_length,
    "h1_present": _page_h1_present,
    "h1_single": _page_h1_single,
    "heading_hierarchy": _page_heading_hierarchy,
    "canonical": _page_canonical,
    "og_tags": _page_og,
    "og_image": _page_og_image,
    "viewport": _page_viewport,
    "charset": _page_charset,
    "lang": _page_lang,
    "alt_coverage": _page_alt,
    "content_length": _page_words,
    "internal_links": _page_internal_links,
    "json_ld": _page_json_ld,
    "questions": _page_questions,
    "dates": _page_dates,
    "facts": _page_facts,
    "lists_tables": _page_lists_tables,
    "page_size": _page_size,
    "resources": _page_resources,
}


@dataclass
class AuditResult:
    """Scored audit: total, per-category, per-check, prioritised fixes."""

    start_url: str
    total: int
    categories: dict  # category -> (earned_weight, available_weight, pct)
    checks: list  # list[CheckResult] in CHECKS order
    fixes: list  # list[dict] sorted by impact desc
    pages_audited: int
    pages_failed: int
    crawl: CrawlResult = field(repr=False)


def audit(crawl: CrawlResult) -> AuditResult:
    results = []
    for check in CHECKS:
        if check.kind == "site":
            score, detail = SITE_FUNCS[check.id](crawl)
            results.append(CheckResult(check, score, detail))
        else:
            per_page = []
            failing = []
            for page in crawl.pages:
                score, detail = PAGE_FUNCS[check.id](page)
                if score is None:
                    continue
                per_page.append(score)
                if score < 1.0:
                    failing.append(page.url)
            if not per_page:
                results.append(CheckResult(check, None, "no pages to judge"))
            else:
                avg = sum(per_page) / len(per_page)
                n_fail = len(failing)
                detail = (f"{len(per_page) - n_fail}/{len(per_page)} page(s) pass"
                          if n_fail else f"all {len(per_page)} page(s) pass")
                results.append(CheckResult(check, round(avg, 3), detail,
                                           failing_pages=sorted(failing)))

    total_avail = sum(c.check.weight for c in results if c.score is not None)
    total_earn = sum(c.check.weight * c.score for c in results if c.score is not None)
    total = round(100 * total_earn / total_avail) if total_avail else 0

    categories = {}
    for cat in ("Technical", "On-page SEO", "Links", "GEO"):
        avail = sum(c.check.weight for c in results
                    if c.check.category == cat and c.score is not None)
        earn = sum(c.check.weight * c.score for c in results
                   if c.check.category == cat and c.score is not None)
        pct = round(100 * earn / avail) if avail else 0
        categories[cat] = (round(earn, 1), avail, pct)

    fixes = []
    for c in results:
        if c.score is None or c.score >= 1.0:
            continue
        impact = round(c.check.weight * (1 - c.score), 1)
        fixes.append({
            "check": c.check.id,
            "title": c.check.title,
            "category": c.check.category,
            "impact": impact,
            "fix": c.check.fix,
            "detail": c.detail,
            "pages": c.failing_pages[:3],
        })
    fixes.sort(key=lambda f: (-f["impact"], f["check"]))

    return AuditResult(
        start_url=crawl.start_url,
        total=total,
        categories=categories,
        checks=results,
        fixes=fixes,
        pages_audited=len(crawl.pages),
        pages_failed=len(crawl.failed_pages),
        crawl=crawl,
    )


def grade(total: int) -> str:
    if total >= 90:
        return "A — excellent"
    if total >= 75:
        return "B — good"
    if total >= 60:
        return "C — needs work"
    if total >= 40:
        return "D — poor"
    return "F — critical"
