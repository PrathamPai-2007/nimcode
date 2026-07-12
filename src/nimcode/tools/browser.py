import base64
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import resolve_and_validate_path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@register_tool(
    "screenshot_page",
    schema={
        "type": "function",
        "function": {
            "name": "screenshot_page",
            "description": "Renders a local URL or static HTML file using Playwright (Chromium) and returns a base64 encoded screenshot. Use this to visually verify the frontend/UI. Requires `playwright install chromium` to be run once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to render (e.g. http://localhost:3000). Provide this OR html_file.",
                    },
                    "html_file": {
                        "type": "string",
                        "description": "The path to a static HTML file to render, relative to workspace root.",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "If true, takes a screenshot of the full scrollable page. Defaults to false.",
                    },
                },
                "anyOf": [{"required": ["url"]}, {"required": ["html_file"]}],
            },
        },
    },
)
def screenshot_page(args: Dict[str, Any], workspace_root: Path, config: Any) -> str:
    if sync_playwright is None:
        return "Error: playwright is not installed. Please add playwright to your dependencies and run `playwright install chromium`."

    url = args.get("url")
    html_file = args.get("html_file")
    full_page = args.get("full_page", False)

    if not url and not html_file:
        return "Error: Must provide either 'url' or 'html_file'."

    target_url = url
    if html_file:
        try:
            target_path = resolve_and_validate_path(html_file, workspace_root)
            if not target_path.exists():
                return f"Error: HTML file {html_file} does not exist."
            target_url = target_path.absolute().as_uri()
        except Exception as e:
            return f"Error resolving path: {e}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # Navigate to the URL
            response = page.goto(target_url, wait_until="networkidle", timeout=15000)

            # Ensure it didn't fail
            if response and not response.ok and not target_url.startswith("file://"):
                pass  # Just proceed to take screenshot even if error code

            screenshot_bytes = page.screenshot(full_page=full_page)
            browser.close()

            b64_img = base64.b64encode(screenshot_bytes).decode("utf-8")
            return f"Screenshot captured successfully. Base64 data:\ndata:image/png;base64,{b64_img}"
    except Exception as e:
        return f"Error capturing screenshot: {e}"
