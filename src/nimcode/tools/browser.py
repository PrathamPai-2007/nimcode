import base64
import time
from pathlib import Path
from typing import Dict, Any
from nimcode.tools.registry import register_tool
from nimcode.tools.base import (  # noqa: F401  (re-exported for callers/tests)
    resolve_and_validate_path,
    _LOCAL_HOSTS,
    _check_url,
    _ip_is_internal,
    _is_url_allowed,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore


def _make_request_guard(workspace_root, blocked: list):
    """Playwright route handler vetting EVERY request the page makes
    (redirect hops, iframes, subresources, fetch/XHR), not just the first URL."""
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    root = Path(workspace_root).resolve() if workspace_root is not None else None

    def _allowed(url: str) -> bool:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("data", "blob", "about"):
            return True
        if scheme == "file":
            # Local HTML may only pull files from inside the workspace.
            try:
                path = Path(url2pathname(urlparse(url).path)).resolve()
                return root is not None and path.is_relative_to(root)
            except Exception:
                return False
        return _is_url_allowed(url)[0]

    def _guard(route, request):
        if _allowed(request.url):
            route.continue_()
        else:
            blocked.append(request.url)
            route.abort()

    return _guard


def _fetch_with_retry(
    url: str,
    max_retries: int = 3,
    base_timeout: int = 15000,
    full_page: bool = False,
    workspace_root: Any = None,
    pinned_ips: Any = None,
) -> Dict[str, Any]:
    """
    Navigate to URL and screenshot inside the playwright context so the driver
    is always stopped (no leak) and no dangling page is returned.
    Returns dict with success, image_bytes, error.
    """
    if sync_playwright is None:
        return {"success": False, "error": "playwright not installed"}

    from urllib.parse import urlparse

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    host = (urlparse(url).hostname or "").lower()
    if pinned_ips and host not in _LOCAL_HOSTS:
        # DNS-rebinding guard: Chromium must connect to the IP we vetted, not
        # re-resolve the name to something internal.
        ip = pinned_ips[0]
        ip_s = f"[{ip}]" if ip.version == 6 else str(ip)
        launch_args.append(f"--host-resolver-rules=MAP {host} {ip_s}")

    last_error = None
    for attempt in range(max_retries):
        blocked: list = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=launch_args)
                try:
                    # Block service workers so every request hits the route guard.
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        service_workers="block",
                    )
                    context.route("**/*", _make_request_guard(workspace_root, blocked))
                    page = context.new_page()
                    timeout = base_timeout + (attempt * 5000)
                    try:
                        response = page.goto(
                            url, wait_until="domcontentloaded", timeout=timeout
                        )
                    except Exception:
                        if blocked:
                            return {
                                "success": False,
                                "error": f"blocked request to {blocked[0]} (SSRF guard)",
                            }
                        raise
                    final_ok, _ = (
                        (True, "")
                        if page.url.startswith(("file:", "about:", "data:"))
                        else _is_url_allowed(page.url)
                    )
                    if not final_ok:
                        return {
                            "success": False,
                            "error": f"navigation ended at blocked URL {page.url}",
                        }
                    if response and not response.ok and not url.startswith("file://"):
                        error_msg = f"HTTP {response.status}: {response.status_text}"
                        if attempt < max_retries - 1:
                            time.sleep(1 + attempt)
                            continue
                        return {"success": False, "error": error_msg}
                    # Fixed settle for dynamic content (networkidle hangs on HMR/polling).
                    try:
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    shot = page.screenshot(full_page=bool(full_page))
                    return {"success": True, "image_bytes": shot}
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1 + attempt)
                continue
            return {"success": False, "error": str(e)}

    return {"success": False, "error": str(last_error)}


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
    if isinstance(full_page, str):
        full_page = full_page.strip().lower() in ("1", "true", "yes", "y", "on")
    else:
        full_page = bool(full_page)

    if not url and not html_file:
        return "Error: Must provide either 'url' or 'html_file'."

    target_url: str = str(url) if url is not None else ""
    pinned_ips: list = []
    if target_url and not html_file:
        ok, reason, pinned_ips = _check_url(target_url)
        if not ok:
            return f"Error: URL blocked for security: {reason}."
    if html_file:
        if not isinstance(html_file, str):
            return "Error: 'html_file' must be a string path."
        try:
            target_path = resolve_and_validate_path(html_file, workspace_root)
            if not target_path.exists():
                return f"Error: HTML file {html_file} does not exist."
            target_url = target_path.absolute().as_uri()
        except Exception as e:
            return f"Error resolving path: {e}"

    if not target_url:
        return "Error: Could not resolve target URL."

    result = _fetch_with_retry(
        target_url,
        full_page=full_page,
        workspace_root=workspace_root,
        pinned_ips=pinned_ips,
    )

    if not result["success"]:
        return f"Error navigating to {target_url}: {result['error']}"

    try:
        screenshot_bytes: bytes = result["image_bytes"]
        # Persist full PNG to project state dir; inline only a capped preview
        # to avoid blowing the model context window (~700KB -> ~175k tokens).
        saved_path = ""
        try:
            from nimcode.config import get_project_nimcode_dir

            shot_dir = get_project_nimcode_dir(workspace_root) / "screenshots"
            shot_dir.mkdir(parents=True, exist_ok=True)
            import time as _time

            saved_path = str(shot_dir / f"screenshot_{int(_time.time() * 1000)}.png")
            with open(saved_path, "wb") as fh:
                fh.write(screenshot_bytes)
        except Exception:
            saved_path = ""
        cap = 100_000
        b64_img = base64.b64encode(screenshot_bytes).decode("utf-8")
        loc = f"Saved to: {saved_path}\n" if saved_path else ""
        if len(b64_img) > cap:
            # A cut-off base64 PNG is undecodable; it only burned ~25k tokens.
            return (
                f"Screenshot captured successfully. {loc}"
                f"[inline base64 omitted: {len(b64_img)} chars exceeds {cap}]"
            )
        return (
            f"Screenshot captured successfully. {loc}"
            f"Base64 preview:\ndata:image/png;base64,{b64_img}"
        )
    except Exception as e:
        return f"Error capturing screenshot: {e}"
