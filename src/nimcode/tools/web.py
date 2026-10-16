import urllib.request
import urllib.parse
import re
import html as html_lib
import time
import threading
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool


def _fetch_with_retry(
    url: str, headers: dict, max_retries: int = 3, timeout: int = 10
) -> str:
    """Fetch URL with exponential backoff retries."""
    last_error = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                # Cap the body: a misbehaving endpoint can't exhaust memory.
                return response.read(5_000_000).decode("utf-8", errors="replace")
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep((2**attempt) * 0.5)
    raise last_error or RuntimeError("fetch failed")


def _fetch_with_overall_timeout(
    url: str,
    headers: dict,
    max_retries: int = 3,
    timeout: int = 15,
    overall_timeout: int = 30,
) -> str:
    """Fetch URL with overall timeout using threading."""
    result = {"html": None, "error": None}

    def fetch():
        try:
            result["html"] = _fetch_with_retry(url, headers, max_retries, timeout)
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()
    thread.join(timeout=overall_timeout)

    if thread.is_alive():
        raise TimeoutError(f"Web search timed out after {overall_timeout}s")

    if result["error"]:
        raise result["error"]

    return result["html"]


_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL | re.IGNORECASE)


def _clean_text(fragment: str) -> str:
    """Strip tags, decode entities (&amp; &#x27; ...), collapse whitespace."""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _parse_duckduckgo_results(html: str, max_results: int) -> list:
    """Parse DuckDuckGo HTML results with multiple fallback strategies."""
    results = []

    # Strategy 1: Modern DDG layout
    blocks = html.split(
        '<div class="result results_links results_links_deep web-result ">'
    )
    if len(blocks) <= 1:
        # Strategy 2: Alternative layout
        blocks = html.split('<div class="links_main links_deep result__body">')
    if len(blocks) <= 1:
        # Strategy 3: Generic result container
        blocks = re.split(r'<div class="[^"]*result[^"]*"[^>]*>', html)
    if len(blocks) <= 1:
        # Strategy 4: Look for any link with snippet
        blocks = re.split(r'<a class="result__snippet"', html)

    for block in blocks[1:]:
        if len(results) >= max_results:
            break

        # Collect anchors with their own attributes (class/href order varies).
        anchors = []
        for m in _ANCHOR_RE.finditer(block):
            attrs = m.group(1)
            cls = re.search(r'class="([^"]*)"', attrs)
            href_m = re.search(r'href="([^"]*)"', attrs)
            anchors.append(
                (cls.group(1) if cls else "", href_m.group(1) if href_m else "", m)
            )

        def _pick(css: str):
            return next((a for a in anchors if css in a[0].split() and a[1]), None)

        # Prefer the real title link (result__a) over the display URL (result__url).
        link = (
            _pick("result__a")
            or _pick("result__url")
            or next((a for a in anchors if a[1]), None)
        )
        if not link:
            continue
        title = _clean_text(link[2].group(2))
        href = html_lib.unescape(link[1])
        snippet_match = re.search(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td|span)>',
            block,
            re.DOTALL,
        )
        snippet = _clean_text(snippet_match.group(1)) if snippet_match else ""

        # Clean up redirect URLs
        if "uddg=" in href:
            queries = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "uddg" in queries:
                href = queries["uddg"][0]
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/y.js" in href:
            continue  # sponsored result

        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})

    return results


@register_tool(
    "web_search",
    schema={
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Performs a web search for a given query to retrieve documentation, references, or debug information from the web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to perform.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "The maximum number of search results to return. Defaults to 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
)
def web_search(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    from nimcode.tools.base import truncate_output

    query = args.get("query")
    if not query or not isinstance(query, str):
        return "Error: No query provided."

    try:
        max_results = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(10, max_results))

    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        html = _fetch_with_overall_timeout(
            url, headers, max_retries=3, timeout=15, overall_timeout=30
        )
    except Exception as e:
        return f"Error fetching search results after retries: {e}"
    if not html:
        return "No results found (empty response)."

    try:
        results = _parse_duckduckgo_results(html, max_results)
    except Exception as e:
        return f"Error parsing search results: {e}"

    if not results:
        if "anomaly" in html.lower() or "captcha" in html.lower():
            return "No results: DuckDuckGo served a bot-check page; retry later."
        return "No results found."

    output = []
    for i, r in enumerate(results, 1):
        output.append(
            f"{i}. {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}\n"
        )
    return truncate_output("\n".join(output), 20000)
