"""Streamlit web app for the SEO + GEO audit CLI.

Paste a URL, run the audit in-process, see the score, the category
breakdown, the checks table and the prioritised fix list. No API key,
no account — the audit runs locally against the site you enter.
"""

import streamlit as st

from seo_audit.checks import audit
from seo_audit.crawler import Crawler

st.set_page_config(page_title="SEO + GEO Site Audit", layout="wide")
st.title("SEO + GEO Site Audit")
st.write("Free technical SEO and AI-search readiness audit. "
         "No API key, no account — the audit runs locally.")

url = st.text_input("Site URL", "https://example.com")
max_pages = st.slider("Max pages", 1, 100, 20)
max_depth = st.slider("Max depth", 0, 4, 2)
check_external = st.checkbox("Check external links too", value=True)

if st.button("Run audit"):
    if not url.startswith(("http://", "https://")):
        st.error("URL must start with http:// or https://")
        st.stop()
    with st.spinner("Crawling and auditing…"):
        try:
            crawler = Crawler(url, max_pages=max_pages, max_depth=max_depth,
                              check_external=check_external)
            crawl = crawler.crawl()
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        if not crawl.pages:
            st.error("Start page unreachable — check the URL and try again.")
            st.stop()
        result = audit(crawl)

    st.header(f"Score: {result.total}/100")
    for cat, (_, _, pct) in result.categories.items():
        st.write(f"**{cat}**: {pct}/100")
        st.progress(pct / 100)
    st.subheader(f"Pages audited: {result.pages_audited} · "
                 f"failed to load: {result.pages_failed}")

    st.subheader("Top fixes (highest impact first)")
    for fix in result.fixes:
        st.write(f"- **{fix['title']}** ({fix['category']}, impact {fix['impact']}): "
                 f"{fix['fix']} — _{fix['detail']}_")

    st.subheader("All checks")
    rows = [{
        "Check": c.check.title,
        "Category": c.check.category,
        "Result": "PASS" if c.score == 1.0 else ("n/a" if c.score is None
                  else f"{c.score:.0%}"),
        "Detail": c.detail,
    } for c in result.checks]
    st.table(rows)
