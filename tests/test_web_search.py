from unittest.mock import patch, MagicMock
from pathlib import Path
from nimcode.tools.registry import TOOL_REGISTRY, AVAILABLE_TOOLS_SPEC
from nimcode.tools.web import web_search


def test_web_search_registered():
    assert "web_search" in TOOL_REGISTRY
    schema = next(
        (s for s in AVAILABLE_TOOLS_SPEC if s["function"]["name"] == "web_search"), None
    )
    assert schema is not None
    assert "query" in schema["function"]["parameters"]["required"]


@patch("urllib.request.urlopen")
def test_web_search_logic(mock_urlopen):
    # Mock DDG HTML response
    mock_response = MagicMock()
    mock_response.read.return_value = b"""
    <html>
    <body>
    <div class="result results_links results_links_deep web-result ">
        <a class="result__url" href="https://example.com/test">Test Title</a>
        <a class="result__snippet" href="https://example.com/test">This is a test snippet.</a>
    </div>
    </body>
    </html>
    """
    mock_urlopen.return_value.__enter__.return_value = mock_response

    args = {"query": "test query", "max_results": 1}
    result = web_search(args, Path("."), None)

    assert "1. Test Title" in result
    assert "URL: https://example.com/test" in result
    assert "Snippet: This is a test snippet." in result


def test_web_search_missing_query():
    result = web_search({}, Path("."), None)
    assert "Error" in result
