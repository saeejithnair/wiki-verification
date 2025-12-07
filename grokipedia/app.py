#!/usr/bin/env python3
"""
Grokipedia Frontend - Custom viewer for Grokipedia articles with enhanced citations.

Performance-optimized architecture:
- Article content cached in Supabase (avoid re-fetching from grokipedia.com)
- Batch queries for citation cache lookups
- Background async processing for LLM calls
- Chunked streaming for large citation sets
- No blocking on LLM - page loads instantly, citations stream in
"""

import asyncio
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests
import markdown
from bs4 import BeautifulSoup
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# xAI SDK imports
from xai_sdk import AsyncClient
from xai_sdk.chat import user
from xai_sdk.tools import web_search

# Supabase for persistent caching
from supabase import create_client

app = Flask(__name__)

# Initialize Supabase client
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# Thread pool for background LLM processing
# Keep low to avoid connection exhaustion on macOS (gRPC + Supabase)
executor = ThreadPoolExecutor(max_workers=4)

# Semaphore to limit concurrent Supabase operations
import threading
supabase_semaphore = threading.Semaphore(3)

# In-memory cache for articles (LRU-style, keeps last 100)
article_memory_cache = {}
ARTICLE_CACHE_MAX = 100


def supabase_with_retry(operation, max_retries=3):
    """Execute a Supabase operation with retry logic and connection limiting."""
    last_error = None
    for attempt in range(max_retries):
        try:
            with supabase_semaphore:
                return operation()
        except Exception as e:
            last_error = e
            error_str = str(e)
            # Only retry on connection errors
            if 'Resource temporarily unavailable' in error_str or \
               'ConnectionTerminated' in error_str or \
               'Connection' in error_str:
                time.sleep(0.1 * (attempt + 1))  # Backoff
                continue
            else:
                raise  # Don't retry on other errors
    # If we exhausted retries, raise the last error
    raise last_error

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# =============================================================================
# ARTICLE CACHING
# =============================================================================

def get_cached_article(page_name: str) -> dict | None:
    """Check memory cache first, then Supabase for cached article."""
    if page_name in article_memory_cache:
        return article_memory_cache[page_name]

    try:
        def _query():
            return supabase.table("article_cache").select(
                "markdown, rendered_html, citations, fetched_at"
            ).eq("page_name", page_name).limit(1).execute()

        result = supabase_with_retry(_query)
        if result.data:
            row = result.data[0]
            article = {
                "markdown": row["markdown"],
                "rendered_html": row["rendered_html"],
                "citations": json.loads(row["citations"]) if isinstance(row["citations"], str) else row["citations"],
                "page_name": page_name,
                "url": f"https://grokipedia.com/page/{page_name}",
            }
            article_memory_cache[page_name] = article
            if len(article_memory_cache) > ARTICLE_CACHE_MAX:
                oldest = next(iter(article_memory_cache))
                del article_memory_cache[oldest]
            return article
    except Exception as e:
        if 'Resource temporarily' not in str(e) and 'Connection' not in str(e):
            print(f"Article cache lookup error: {e}")
    return None


def cache_article(page_name: str, markdown_content: str, rendered_html: str, citations: list):
    """Store article in Supabase cache."""
    try:
        def _upsert():
            return supabase.table("article_cache").upsert({
                "page_name": page_name,
                "markdown": markdown_content,
                "rendered_html": rendered_html,
                "citations": json.dumps(citations),
                "fetched_at": "now()",
            }).execute()

        supabase_with_retry(_upsert)
        article_memory_cache[page_name] = {
            "markdown": markdown_content,
            "rendered_html": rendered_html,
            "citations": citations,
            "page_name": page_name,
            "url": f"https://grokipedia.com/page/{page_name}",
        }
    except Exception as e:
        if 'Resource temporarily' not in str(e) and 'Connection' not in str(e):
            print(f"Article cache write error: {e}")


# =============================================================================
# CITATION CACHING - BATCH OPERATIONS
# =============================================================================

def get_cached_citations_batch(citation_keys: list[tuple[str, str]]) -> dict:
    """Batch lookup for multiple citations at once."""
    if not citation_keys:
        return {}

    results = {}
    try:
        urls = list(set(url for url, _ in citation_keys))

        def _query():
            return supabase.table("citation_cache").select(
                "source_url, claim_context, relevant_quote, summary"
            ).in_("source_url", urls).execute()

        response = supabase_with_retry(_query)
        for row in response.data:
            key = (row["source_url"], row["claim_context"])
            results[key] = {
                "context": row["relevant_quote"],
                "summary": row["summary"],
            }
    except Exception as e:
        # Only log non-connection errors
        if 'Resource temporarily' not in str(e) and 'Connection' not in str(e):
            print(f"Batch citation cache lookup error: {e}")

    return results


def get_cached_citation(source_url: str, claim_context: str) -> dict | None:
    """Check Supabase for cached citation analysis."""
    try:
        def _query():
            return supabase.table("citation_cache").select("relevant_quote, summary, title").eq(
                "source_url", source_url
            ).eq(
                "claim_context", claim_context
            ).limit(1).execute()

        result = supabase_with_retry(_query)
        if result.data:
            return {
                "title": result.data[0].get("title", ""),
                "context": result.data[0]["relevant_quote"],
                "summary": result.data[0]["summary"],
            }
    except Exception as e:
        # Only log non-connection errors (connection errors are retried)
        if 'Resource temporarily' not in str(e) and 'Connection' not in str(e):
            print(f"Supabase cache lookup error: {e}")
    return None


def cache_citation(source_url: str, claim_context: str, relevant_quote: str, summary: str, title: str = ""):
    """Store citation analysis in Supabase."""
    try:
        def _upsert():
            return supabase.table("citation_cache").upsert({
                "source_url": source_url,
                "claim_context": claim_context,
                "relevant_quote": relevant_quote,
                "summary": summary,
                "title": title,
            }).execute()

        supabase_with_retry(_upsert)
    except Exception as e:
        # Ignore duplicate key errors (race condition, data already exists)
        if '23505' not in str(e) and 'duplicate key' not in str(e).lower():
            # Only log non-connection errors
            if 'Resource temporarily' not in str(e) and 'Connection' not in str(e):
                print(f"Supabase cache write error: {e}")


# =============================================================================
# ARTICLE FETCHING & PARSING
# =============================================================================

def fetch_grokipedia_page(page_name: str, force_refresh: bool = False) -> dict:
    """Fetch a Grokipedia page with caching."""
    if not force_refresh:
        cached = get_cached_article(page_name)
        if cached:
            return cached

    url = f"https://grokipedia.com/page/{page_name}"
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    html = response.text

    pattern = r'self\.__next_f\.push\(\[1,"(.+?)"\]\)</script>'
    chunks = re.findall(pattern, html)

    markdown_content = ""
    for chunk in chunks:
        if chunk.startswith("# ") or "\\n# " in chunk[:100]:
            markdown_content = chunk
            break

    if markdown_content:
        markdown_content = markdown_content.replace("\\n", "\n")
        markdown_content = markdown_content.replace("\\t", "\t")
        markdown_content = markdown_content.replace('\\"', '"')
        markdown_content = markdown_content.replace("\\'", "'")
        markdown_content = markdown_content.replace("\\\\", "\\")

    citations = []
    for chunk in chunks:
        if '\\"citations\\":[{' in chunk:
            start_marker = '\\"citations\\":['
            start_idx = chunk.find(start_marker)
            if start_idx != -1:
                start_idx += len(start_marker) - 1
                bracket_count = 0
                end_idx = start_idx
                i = start_idx
                while i < len(chunk):
                    char = chunk[i]
                    if char == '\\' and i + 1 < len(chunk):
                        i += 2
                        continue
                    if char == '[':
                        bracket_count += 1
                    elif char == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            end_idx = i + 1
                            break
                    i += 1

                citations_str = chunk[start_idx:end_idx]
                citations_str = citations_str.replace('\\"', '"')
                citations_str = citations_str.replace('\\\\', '\\')
                try:
                    citations = json.loads(citations_str)
                except json.JSONDecodeError:
                    pass
            break

    rendered_html = extract_rendered_article(html, citations, page_name)
    cache_article(page_name, markdown_content, rendered_html, citations)

    return {
        "markdown": markdown_content,
        "rendered_html": rendered_html,
        "citations": citations,
        "url": url,
        "page_name": page_name,
    }


def extract_rendered_article(html: str, citations: list, page_name: str = "") -> str:
    """Extract the rendered article HTML from Grokipedia page."""
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article") or soup.find("div", class_=re.compile(r"prose"))

    if not article:
        main = soup.find("main")
        if main:
            article = main

    if not article:
        return ""

    # Build citation lookup from the JSON data first
    citation_lookup = {c["id"]: c for c in citations}

    # IMPORTANT: Extract correct URLs from embedded references list
    # Grokipedia's citations JSON has mismatched data, but the embedded <ol>
    # references list at the bottom is correctly ordered by position
    references_section = article.find("div", id="references")
    if references_section:
        ref_ol = references_section.find("ol")
        if ref_ol:
            for idx, li in enumerate(ref_ol.find_all("li", recursive=False), start=1):
                link = li.find("a", href=True)
                if link:
                    correct_url = link.get("href", "")
                    citation_id = str(idx)
                    # Update or create citation with correct URL
                    if citation_id in citation_lookup:
                        old_url = citation_lookup[citation_id].get("url", "")
                        # Fix the URL but keep existing title/description
                        # (slightly mismatched metadata is better than "Untitled Source")
                        if old_url != correct_url:
                            citation_lookup[citation_id]["url"] = correct_url
                    else:
                        citation_lookup[citation_id] = {
                            "id": citation_id,
                            "url": correct_url,
                            "title": "",
                            "description": ""
                        }
        # Remove the embedded references section - we'll render our own
        references_section.decompose()

    # Fix KaTeX math rendering that corrupts currency values like "$200–$450"
    # KaTeX interprets $ as LaTeX math delimiters, creating MathML gibberish
    # The annotation often contains broken citations like [](url) that need fixing
    def fix_katex_text(text):
        """Fix broken citations in KaTeX annotation text."""
        # Build URL to citation number mapping
        url_to_id = {c.get("url", ""): c.get("id", "") for c in citations if c.get("url")}

        # Replace [](url) patterns with proper [N] citations
        def replace_broken_citation(match):
            url = match.group(1)
            citation_id = url_to_id.get(url, "")
            if citation_id:
                return f"[{citation_id}]"
            return ""  # Remove if we can't find the citation

        fixed = re.sub(r'\[\]\(([^)]+)\)', replace_broken_citation, text)
        return fixed

    for katex_span in article.find_all("span", class_="katex"):
        annotation = katex_span.find("annotation")
        if annotation:
            original_text = annotation.get_text()
            fixed_text = fix_katex_text(original_text)
            # Only add $ prefix if it looks like a currency value
            if re.match(r'^\d', fixed_text):
                fixed_text = f"${fixed_text}"
            katex_span.replace_with(fixed_text)
        else:
            katex_span.decompose()

    # Also remove any standalone math elements
    for math_elem in article.find_all("math"):
        annotation = math_elem.find("annotation")
        if annotation:
            original_text = annotation.get_text()
            fixed_text = fix_katex_text(original_text)
            if re.match(r'^\d', fixed_text):
                fixed_text = f"${fixed_text}"
            math_elem.replace_with(fixed_text)
        else:
            math_elem.decompose()

    # Remove Grokipedia-specific UI elements we don't want
    # 1. Remove any "fact-checked" badges/buttons from Grokipedia
    for elem in article.find_all(string=re.compile(r'[Ff]act.?check', re.IGNORECASE)):
        parent = elem.parent
        # Walk up to find the container (usually a div or button)
        for _ in range(5):
            if parent and parent.name in ['div', 'button', 'span', 'aside']:
                # Check if this is a badge-like container (small, not main content)
                if parent.name in ['button', 'aside'] or (
                    parent.get('class') and any('badge' in c or 'fact' in c.lower() for c in parent.get('class', []))
                ):
                    parent.decompose()
                    break
            if parent:
                parent = parent.parent

    # 2. Remove any SVG elements (icons from Grokipedia UI)
    for svg in article.find_all("svg"):
        # Check if it's inside a small container (likely UI element, not content)
        parent = svg.parent
        if parent and parent.name in ['button', 'a', 'span', 'div']:
            # If the parent has minimal text content, it's likely a UI element
            text = parent.get_text(strip=True)
            if len(text) < 50:  # Short text = likely badge/button
                svg.decompose()

    # 3. Remove TOC - can appear before or after h1
    h1 = article.find("h1")

    # Find and remove ALL ul elements that look like TOCs (before content starts)
    for ul in article.find_all("ul"):
        links = ul.find_all("a")
        if links:
            # TOC characteristics: all links are internal (no http), link text matches section names
            all_internal = all(
                not a.get("href", "").startswith("http") and
                not a.get("href", "").startswith("mailto")
                for a in links
            )
            # Check if links point to anchors or are short navigation-like text
            looks_like_toc = all_internal and all(
                len(a.get_text(strip=True)) < 100 for a in links
            )
            if looks_like_toc:
                # Additional check: is this before any real content (h2, p with substantial text)?
                next_content = ul.find_next_sibling()
                if next_content is None or next_content.name in ['h1', 'h2', 'div'] or ul.find_previous('h2') is None:
                    ul.decompose()
                    continue

    # 4. Remove any remaining Grokipedia UI containers (buttons, badges near top)
    # Look for elements that contain "Grok" text and are small UI elements
    for elem in article.find_all(['button', 'aside']):
        elem.decompose()

    # Re-find h1 after cleanup
    h1 = article.find("h1")
    if h1:
        # Remove any existing link icons or UI elements inside h1
        for child in h1.find_all(['a', 'svg', 'button']):
            # Only remove if it's a UI element, not a text link
            if child.name == 'svg' or (child.name == 'a' and not child.get_text(strip=True)):
                child.decompose()

        # Add our own external link icon after h1 text
        original_url = f"https://grokipedia.com/page/{page_name}" if page_name else ""
        link_icon = soup.new_tag("a", href=original_url, target="_blank", title="View on Grokipedia")
        link_icon["class"] = ["external-link-icon"]
        h1.append(link_icon)

    for a in article.find_all("a", href=True):
        href = a["href"]
        if "grokipedia.com/page/" in href:
            match = re.search(r'grokipedia\.com/page/([^/?#]+)', href)
            if match:
                a["href"] = f"/page/{match.group(1)}"

    for sup in article.find_all("sup"):
        text = sup.get_text().strip()
        match = re.match(r'\[(\d+)\]', text)
        if match:
            citation_id = match.group(1)
            citation = citation_lookup.get(citation_id, {})
            sup["class"] = sup.get("class", []) + ["citation"]
            sup["data-id"] = citation_id
            sup["data-title"] = citation.get("title", "")
            sup["data-url"] = citation.get("url", "")
            sup["data-description"] = citation.get("description", "")

    for script in article.find_all(["script", "style"]):
        script.decompose()

    for img in article.find_all("img"):
        img.decompose()

    # Strip inline styles that could cause white backgrounds
    for elem in article.find_all(style=True):
        style = elem.get("style", "")
        # Remove background-related styles
        style = re.sub(r'background[^;]*;?', '', style, flags=re.IGNORECASE)
        style = re.sub(r'box-shadow[^;]*;?', '', style, flags=re.IGNORECASE)
        if style.strip():
            elem["style"] = style.strip()
        else:
            del elem["style"]

    # Convert to string and inject the SVG icon
    result = str(article)
    svg_icon = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>'''
    result = result.replace('class="external-link-icon"></a>', f'class="external-link-icon">{svg_icon}</a>')

    return result


def process_markdown_for_display(md_content: str, citations: list) -> str:
    """Process markdown content for display."""
    citation_lookup = {c["id"]: c for c in citations}
    md_content = re.sub(r'!\[[^\]]*\]\([^)]*(?:\\\)[^)]*)*\)', '', md_content)
    md_content = re.sub(
        r'\[([^\]]+)\]\(https://grokipedia\.com/page/([^)]+)\)',
        r'[\1](/page/\2)',
        md_content
    )
    html = markdown.markdown(md_content, extensions=['extra', 'sane_lists'])

    def replace_citation(match):
        citation_num = match.group(1)
        citation = citation_lookup.get(citation_num, {})
        title = citation.get("title", "").replace('"', '&quot;').replace("'", "&#39;")
        url = citation.get("url", "").replace('"', '&quot;')
        description = citation.get("description", "").replace('"', '&quot;').replace("'", "&#39;")
        return f'<sup class="citation" data-id="{citation_num}" data-title="{title}" data-url="{url}" data-description="{description}">[{citation_num}]</sup>'

    html = re.sub(r'\[(\d+)\]', replace_citation, html)
    return html


# =============================================================================
# LLM ANALYSIS - ASYNC & NON-BLOCKING
# =============================================================================

class CitationAnalysis(BaseModel):
    """Structured output for citation analysis."""
    page_title: str  # The actual title of the source page/article
    relevant_quote: str  # The specific quote from the source that verifies the claim before [X]
    summary: str  # One sentence: "This source confirms [specific claim] by stating [key evidence]"


def _grpc_exception_handler(loop, context):
    """Custom exception handler that suppresses gRPC BlockingIOError on macOS."""
    exception = context.get('exception')
    if isinstance(exception, BlockingIOError):
        # Suppress these - they're non-fatal gRPC completion queue noise on macOS
        return
    # For other exceptions, use default handling
    loop.default_exception_handler(context)


def run_async(coro):
    """Run an async coroutine in a new event loop (for thread pool).

    Uses asyncio.run() which properly manages loop lifecycle and
    handles cleanup to minimize gRPC completion queue conflicts.
    """
    # Create loop manually so we can set custom exception handler
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_grpc_exception_handler)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


async def analyze_citation_async(source_url: str, source_title: str, claim_context: str) -> dict:
    """Use xAI to analyze a citation source."""
    client = AsyncClient()
    chat = client.chat.create(
        model="grok-4-fast",
        tools=[web_search()],
        response_format=CitationAnalysis,
    )

    prompt = f"""I'm fact-checking a Wikipedia-style article. Your task is to verify the specific claim supported by citation [X].

Here is the text (other citations shown as [1], [2], etc. — IGNORE those, focus ONLY on [X]):
"{claim_context}"

The marker [X] appears immediately AFTER the specific fact it supports. Your job:
1. Search and read the source: {source_url}
2. Note the actual page/article title
3. Identify the specific claim RIGHT BEFORE [X] (not claims near other citation numbers)
4. Find a quote that verifies the claim before [X]
5. Explain how the source confirms that specific claim

Source URL: {source_url}

CRITICAL:
- Return the ACTUAL page title from the source (not a made-up title)
- ONLY verify the claim immediately before [X], ignore claims near [1], [2], etc.
- Be specific: "X failed due to Y" → find evidence of failure and reasons
- Don't summarize the general topic — verify the EXACT fact before [X]"""

    chat.append(user(prompt))

    try:
        response = await chat.sample()
        analysis = CitationAnalysis.model_validate_json(response.content)
        return {
            "title": analysis.page_title,
            "relevant_quote": analysis.relevant_quote,
            "summary": analysis.summary,
        }
    except Exception as e:
        return {
            "title": "",
            "relevant_quote": f"Could not extract quote: {str(e)}",
            "summary": "Unable to analyze this citation.",
        }


def analyze_citation_sync(source_url: str, source_title: str, claim_context: str) -> dict:
    """Synchronous wrapper that properly handles async."""
    return run_async(analyze_citation_async(source_url, source_title, claim_context))


# =============================================================================
# ROUTES
# =============================================================================

@app.route("/")
def index():
    """Home page with URL input."""
    return render_template("index.html")


@app.route("/page/<path:page_name>")
def view_page(page_name: str):
    """View a Grokipedia article - loads instantly from cache."""
    try:
        force_refresh = request.args.get("refresh") == "1"
        data = fetch_grokipedia_page(page_name, force_refresh=force_refresh)

        if data["rendered_html"]:
            html_content = data["rendered_html"]
        else:
            html_content = process_markdown_for_display(data["markdown"], data["citations"])

        title_match = re.match(r'^#\s+(.+?)(?:\n|$)', data["markdown"])
        title = title_match.group(1) if title_match else page_name.replace("_", " ")

        return render_template(
            "article.html",
            title=title,
            content=html_content,
            citations=data["citations"],
            original_url=data["url"],
        )
    except requests.exceptions.RequestException as e:
        return render_template("error.html", error=str(e)), 500
    except Exception as e:
        return render_template("error.html", error=str(e)), 500


@app.route("/api/fetch")
def api_fetch():
    """API endpoint to fetch article data."""
    url = request.args.get("url", "")
    match = re.search(r'grokipedia\.com/page/([^/?]+)', url)
    if not match:
        return jsonify({"error": "Invalid Grokipedia URL"}), 400

    page_name = match.group(1)

    try:
        data = fetch_grokipedia_page(page_name)
        html_content = process_markdown_for_display(data["markdown"], data["citations"])
        title_match = re.match(r'^#\s+(.+?)(?:\n|$)', data["markdown"])
        title = title_match.group(1) if title_match else page_name.replace("_", " ")

        return jsonify({
            "title": title,
            "html": html_content,
            "citations": data["citations"],
            "page_name": page_name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/citation/<citation_id>")
def api_citation(citation_id: str):
    """Single citation lookup - check cache, process if needed."""
    source_url = request.args.get("url", "")
    source_title = request.args.get("title", "")
    claim_context = request.args.get("context", "")

    if not source_url:
        return jsonify({
            "id": citation_id,
            "context": "No source URL provided",
            "summary": "Unable to analyze without source URL",
        })

    cached = get_cached_citation(source_url, claim_context)
    if cached:
        return jsonify({
            "id": citation_id,
            "title": cached.get("title", ""),
            "context": cached["context"],
            "summary": cached["summary"],
            "cached": True,
        })

    try:
        analysis = analyze_citation_sync(source_url, source_title, claim_context)
        cache_citation(
            source_url,
            claim_context,
            analysis["relevant_quote"],
            analysis["summary"],
            analysis.get("title", "")
        )

        return jsonify({
            "id": citation_id,
            "title": analysis.get("title", ""),
            "context": analysis["relevant_quote"],
            "summary": analysis["summary"],
            "cached": False,
        })
    except Exception as e:
        return jsonify({
            "id": citation_id,
            "title": "",
            "context": f"Error: {str(e)}",
            "summary": "Failed to analyze citation",
        })


@app.route("/api/citation/feedback", methods=["POST"])
def api_citation_feedback():
    """Store user feedback on citation quality."""
    data = request.get_json()
    citation_id = data.get("citation_id", "")
    source_url = data.get("url", "")
    vote = data.get("vote")  # 'up', 'down', or null
    page = data.get("page", "")

    if not source_url:
        return jsonify({"error": "Missing URL"}), 400

    try:
        def _upsert():
            return supabase.table("citation_feedback").upsert({
                "source_url": source_url,
                "citation_id": citation_id,
                "page_path": page,
                "vote": vote,
                "updated_at": "now()",
            }).execute()

        supabase_with_retry(_upsert)
        return jsonify({"success": True})
    except Exception as e:
        # Log but don't fail - feedback is optional
        print(f"Feedback store error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/citations/batch", methods=["POST"])
def api_citations_batch():
    """
    Batch check which citations are already cached.
    Returns cached results immediately, flags which need processing.
    """
    data = request.get_json()
    citations = data.get("citations", [])

    if not citations:
        return jsonify({"results": {}, "uncached": []})

    keys = [(c["url"], c["context"]) for c in citations if c.get("url")]
    cached = get_cached_citations_batch(keys)

    results = {}
    uncached = []

    for c in citations:
        citation_id = c.get("id")
        url = c.get("url", "")
        context = c.get("context", "")

        if not url:
            results[citation_id] = {
                "context": "No source URL",
                "summary": "Unable to analyze",
                "cached": True,
            }
            continue

        key = (url, context)
        if key in cached:
            results[citation_id] = {
                "context": cached[key]["context"],
                "summary": cached[key]["summary"],
                "cached": True,
            }
        else:
            uncached.append(c)

    return jsonify({
        "results": results,
        "uncached": uncached,
    })


@app.route("/api/citations/stream", methods=["POST"])
def api_citations_stream():
    """
    Server-Sent Events endpoint for streaming citation analysis results.
    Uses POST to handle large payloads. Processes in parallel batches.
    """
    data = request.get_json()
    citations = data.get("citations", [])

    @stream_with_context
    def generate():
        if not citations:
            yield f"data: {json.dumps({'done': True, 'total': 0})}\n\n"
            return

        total = len(citations)
        processed = 0
        start_time = time.time()

        # First, quickly return any cached citations
        for c in citations:
            citation_id = c.get("id")
            url = c.get("url", "")
            context = c.get("context", "")

            if not url:
                processed += 1
                yield f"data: {json.dumps({'id': citation_id, 'context': 'No URL', 'summary': 'N/A', 'error': True, 'processed': processed, 'total': total})}\n\n"
                continue

            cached = get_cached_citation(url, context)
            if cached:
                processed += 1
                yield f"data: {json.dumps({'id': citation_id, 'context': cached['context'], 'summary': cached['summary'], 'cached': True, 'processed': processed, 'total': total})}\n\n"

        # Collect uncached citations
        uncached = []
        for c in citations:
            url = c.get("url", "")
            context = c.get("context", "")
            if url and not get_cached_citation(url, context):
                uncached.append(c)

        if not uncached:
            yield f"data: {json.dumps({'done': True, 'total': total, 'processed': processed})}\n\n"
            return

        # Process uncached citations in parallel using thread pool
        futures = {}
        for c in uncached:
            citation_id = c.get("id")
            url = c.get("url", "")
            title = c.get("title", "")
            context = c.get("context", "")

            future = executor.submit(analyze_citation_sync, url, title, context)
            futures[future] = (citation_id, url, context)

        # Yield results as they complete
        for future in as_completed(futures):
            citation_id, url, context = futures[future]
            processed += 1

            try:
                result = future.result(timeout=120)
                cache_citation(url, context, result["relevant_quote"], result["summary"])

                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = remaining / rate if rate > 0 else 0

                yield f"data: {json.dumps({'id': citation_id, 'context': result['relevant_quote'], 'summary': result['summary'], 'cached': False, 'processed': processed, 'total': total, 'eta': round(eta)})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'id': citation_id, 'context': f'Error: {str(e)}', 'summary': 'Failed', 'error': True, 'processed': processed, 'total': total})}\n\n"

        yield f"data: {json.dumps({'done': True, 'total': total, 'processed': processed})}\n\n"

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',  # Disable nginx buffering
    })


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True)

