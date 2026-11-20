"""Same-domain crawler: BFS over HTML pages, robots.txt aware.

Only the site being audited is fetched. External links are checked with
HEAD (GET fallback) but never crawled. No other network traffic happens.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

USER_AGENT = "seo-geo-audit-cli/1.0 (+https://github.com/mrabr381/seo-geo-audit-cli)"

# Schemes / prefixes that are not pages and must never be crawled.
SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "#")


@dataclass
class Page:
    """One fetched HTML page, with everything the checks need."""

    url: str
    status: int
    final_url: str
    size_bytes: int
    elapsed_s: float
    html: str = ""
    soup: object = None  # BeautifulSoup, set after fetch
    links_internal: list = field(default_factory=list)
    links_external: list = field(default_factory=list)
    error: str = ""


@dataclass
class CrawlResult:
    """Everything a crawl found, in deterministic (sorted) order."""

    start_url: str
    pages: list = field(default_factory=list)  # list[Page], sorted by url
    failed_pages: list = field(default_factory=list)  # list[dict]
    external_status: dict = field(default_factory=dict)  # url -> (status|None, note)
    robots_txt_ok: bool = False
    sitemap_ok: bool = False
    sitemap_urls: list = field(default_factory=list)
    llms_txt_ok: bool = False
    llms_txt_text: str = ""


def _normalise(url: str) -> str:
    """Strip fragments; collapse /index.html to its directory.

    Without this, / and /index.html are crawled as two different pages
    with identical content — which also trips the unique-titles check.
    """
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    elif path.endswith("/index.htm"):
        path = path[: -len("index.htm")]
    if path == "/":
        path = ""
    else:
        path = path.rstrip("/")
    return parsed._replace(path=path).geturl()


def _same_origin(url: str, origin: str) -> bool:
    return urlparse(url).netloc.lower() == origin


def _is_html(content_type: str) -> bool:
    return "text/html" in (content_type or "").lower()


class Crawler:
    def __init__(self, start_url: str, max_pages: int = 50, max_depth: int = 3,
                 timeout: float = 10.0, check_external: bool = True):
        self.start_url = _normalise(start_url)
        self.origin = urlparse(self.start_url).netloc.lower()
        self.scheme = urlparse(self.start_url).scheme
        if self.scheme not in ("http", "https"):
            raise ValueError(f"URL must start with http:// or https://, got: {start_url}")
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.timeout = timeout
        self.check_external = check_external
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # -- robots ---------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        rp = RobotFileParser()
        robots_url = f"{self.scheme}://{self.origin}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code != 200:
                return True  # no usable robots.txt -> allow all
            rp.parse(resp.text.splitlines())
            return rp.can_fetch(USER_AGENT, url)
        except requests.RequestException:
            return True  # robots.txt unreachable -> allow all, don't block audit

    # -- fetching -------------------------------------------------------
    def _fetch(self, url: str) -> Page:
        started = time.monotonic()
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise RuntimeError(str(exc)) from exc
        elapsed = time.monotonic() - started
        final = _normalise(resp.url)
        return Page(
            url=_normalise(url),
            status=resp.status_code,
            final_url=final,
            size_bytes=len(resp.content),
            elapsed_s=round(elapsed, 3),
            html=resp.text if _is_html(resp.headers.get("Content-Type", "")) else "",
        )

    def _extract_links(self, page: Page) -> None:
        soup = page.soup
        for a in soup.find_all("a", href=True):
            href = (a["href"] or "").strip()
            if not href or href.startswith(SKIP_PREFIXES):
                continue
            abs_url = _normalise(urljoin(page.final_url, href))
            if not abs_url.startswith(("http://", "https://")):
                continue
            if _same_origin(abs_url, self.origin):
                page.links_internal.append(abs_url)
            else:
                page.links_external.append(abs_url)
        page.links_internal = sorted(set(page.links_internal))
        page.links_external = sorted(set(page.links_external))

    # -- well-known files ------------------------------------------------
    def _well_known(self, result: CrawlResult) -> None:
        base = f"{self.scheme}://{self.origin}"
        for path, attr in (("/robots.txt", "robots_txt_ok"),
                           ("/sitemap.xml", "sitemap_ok"),
                           ("/llms.txt", "llms_txt_ok")):
            try:
                resp = self.session.get(base + path, timeout=self.timeout)
                ok = resp.status_code == 200 and len(resp.content) > 0
                setattr(result, attr, ok)
                if path == "/sitemap.xml" and ok:
                    result.sitemap_urls = self._parse_sitemap(resp.text)
                if path == "/llms.txt" and ok:
                    result.llms_txt_text = resp.text
            except requests.RequestException:
                setattr(result, attr, False)

    @staticmethod
    def _parse_sitemap(xml_text: str) -> list:
        # ElementTree, not BeautifulSoup "xml" — bs4's xml parser needs lxml,
        # which is not installed, and the old code silently returned [].
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        urls = []
        for elem in root.iter():
            if (elem.tag == "loc" or elem.tag.endswith("}loc")) and elem.text:
                urls.append(elem.text.strip())
        return sorted(set(_normalise(u) for u in urls if u.startswith("http")))

    # -- external link checking ------------------------------------------
    def _check_external(self, urls: list) -> dict:
        """HEAD each external URL (GET fallback); returns {url: (status, note)}."""

        def check(url: str):
            try:
                resp = self.session.head(url, timeout=8, allow_redirects=True)
                if resp.status_code in (400, 403, 405):
                    resp = self.session.get(url, timeout=8, stream=True)
                    resp.close()
                if resp.status_code >= 400:
                    return url, (resp.status_code, f"HTTP {resp.status_code}")
                return url, (resp.status_code, "ok")
            except requests.RequestException as exc:
                return url, (None, f"unreachable: {type(exc).__name__}")

        out = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for url, value in pool.map(check, sorted(set(urls))):
                out[url] = value
        return out

    # -- main crawl -------------------------------------------------------
    def crawl(self) -> CrawlResult:
        result = CrawlResult(start_url=self.start_url)
        self._well_known(result)

        queue = [(self.start_url, 0)]
        visited = set()
        pages = []
        failed = []
        external_urls = set()

        while queue and len(pages) + len(failed) < self.max_pages:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            if not self._robots_allows(url):
                failed.append({"url": url, "error": "blocked by robots.txt"})
                continue
            try:
                page = self._fetch(url)
            except RuntimeError as exc:
                failed.append({"url": url, "error": str(exc)})
                continue
            if page.status >= 400:
                failed.append({"url": url, "error": f"HTTP {page.status}"})
                continue
            if not page.html:
                continue  # non-HTML resource: not a page, not an error
            page.soup = BeautifulSoup(page.html, "html.parser")
            self._extract_links(page)
            pages.append(page)
            external_urls.update(page.links_external)
            if depth < self.max_depth:
                for link in page.links_internal:
                    if link not in visited:
                        queue.append((link, depth + 1))

        result.pages = sorted(pages, key=lambda p: p.url)
        result.failed_pages = sorted(failed, key=lambda f: f["url"])
        if self.check_external and external_urls:
            result.external_status = self._check_external(sorted(external_urls))
        return result
