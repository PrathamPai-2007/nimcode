import urllib.request
import urllib.parse
import re
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool


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
    query = args.get("query")
    if not query:
        return "Error: No query provided."

    max_results = args.get("max_results", 5)

    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8")
    except Exception as e:
        return f"Error fetching search results: {e}"

    results = []
    blocks = html.split(
        '<div class="result results_links results_links_deep web-result ">'
    )
    if len(blocks) <= 1:
        blocks = html.split('<div class="links_main links_deep result__body">')

    for block in blocks[1:]:
        if len(results) >= max_results:
            break

        title_match = re.search(
            r'<a class="result__url"[^>]*>(.*?)</a>', block, re.DOTALL
        )
        href_match = re.search(r'href="([^"]+)"', block)
        snippet_match = re.search(
            r'<a class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL
        )

        if title_match and href_match:
            title = title_match.group(1).strip()
            title = re.sub(r"<[^>]+>", "", title)

            href = href_match.group(1)
            if "uddg=" in href:
                parsed = urllib.parse.urlparse(href)
                queries = urllib.parse.parse_qs(parsed.query)
                if "uddg" in queries:
                    href = queries["uddg"][0]

            snippet = ""
            if snippet_match:
                snippet = snippet_match.group(1).strip()
                snippet = re.sub(r"<[^>]+>", "", snippet)

            results.append({"title": title, "url": href, "snippet": snippet})

    if not results:
        return "No results found."

    output = []
    for i, r in enumerate(results, 1):
        output.append(
            f"{i}. {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}\n"
        )
    return "\n".join(output)
