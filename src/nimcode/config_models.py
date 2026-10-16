"""Live model catalog for NVIDIA NIM.

The hosted endpoint exposes OpenAI-compatible ``GET /v1/models`` (callable as
``client.models.list()``), so the app no longer depends solely on the static
``AVAILABLE_MODELS`` table. That endpoint returns ``id``/``owned_by`` but no
context windows (self-hosted NIMs *do* return ``max_model_len``), hence the
curated ``MODEL_CONTEXT_TOKENS`` table below for native windows:

- Nemotron 3 family (Nano/Super/Ultra): 1M native
  (NVIDIA docs + HowAIWorks family breakdown, Jun 2026).
- DeepSeek V4 Flash/Pro: 1M native (contextwindows.dev, Artificial Analysis).
- Mixtral 8x7B Instruct v0.1: 32k (vendor card).
- Anything else: unknown → budget left untouched (never fabricated).

Refresh policy (startup): instant start from cache-or-static, then a daemon
thread refreshes the cache in the background (24h TTL). Offline startup is
unaffected. ``NIMCODE_NO_MODEL_REFRESH=1`` disables background refresh;
``NIMCODE_MODELS_TTL_HOURS`` overrides the TTL.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Native context windows (tokens). Served caps may be lower per provider/tier;
# the agent loop self-corrects via prune-on-overflow retry.
MODEL_CONTEXT_TOKENS: Dict[str, int] = {
    "nvidia/nemotron-3-nano-30b-a3b": 1_000_000,
    "nvidia/nemotron-3-super-120b-a12b": 1_000_000,
    "nvidia/nemotron-3-ultra-550b-a55b": 1_000_000,
    "deepseek-ai/deepseek-v4-flash": 1_000_000,
    "deepseek-ai/deepseek-v4-pro": 1_000_000,
    "mistralai/mixtral-8x7b-instruct-v0.1": 32768,
}

# Pristine default budget — startup auto-apply only touches the budget when it
# still equals this (i.e. the user never customized it). Explicit model
# switches always adjust.
DEFAULT_BUDGET = 128000

# Fallback default model (mirrors Config.fast_model default).
DEFAULT_MODEL_ID = "nvidia/nemotron-3-ultra-550b-a55b"

MODELS_CACHE_TTL_SECONDS = 24 * 3600
MAX_CATALOG_MODELS = 200

# The catalog is chat-oriented; drop retrieval/media/safety endpoints.
SKIP_SUBSTRINGS = (
    "embed",
    "rerank",
    "retrieve",
    "ocr",
    "-vl",
    "vision",
    "tts",
    "asr",
    "stt",
    "guard",
    "safety",
    "moderate",
    "usdcode",
)

_refresh_lock = threading.Lock()
_refresh_inflight = False


def _cache_path() -> Path:
    return Path.home() / ".nimcode" / "models_cache.json"


def _ttl_seconds() -> int:
    try:
        hours = float(os.environ.get("NIMCODE_MODELS_TTL_HOURS", "24"))
        return max(60, int(hours * 3600))
    except (TypeError, ValueError):
        return MODELS_CACHE_TTL_SECONDS


def refresh_disabled() -> bool:
    return os.environ.get("NIMCODE_NO_MODEL_REFRESH", "") == "1"


def pretty_name(model_id: str) -> str:
    """nvidia/nemotron-3-nano-30b-a3b -> Nemotron 3 Nano 30B A3B."""
    slug = model_id.split("/")[-1].replace("_", "-")
    parts = [p for p in slug.split("-") if p]
    out: List[str] = []
    for p in parts:
        if p.isdigit():
            out.append(p)
        elif len(p) == 1 and p.isalpha():
            out.append(p.upper())
        else:
            # Keep version/size tokens readable: v4 -> V4, 30b -> 30B.
            t = p.title()
            out.append(t)
    name = " ".join(out)
    for fix_from, fix_to in (
        ("Ai ", "AI "),
        ("Glm", "GLM"),
        ("Deepseek", "DeepSeek"),
        ("Mixtral", "Mixtral"),
        ("Minimax", "MiniMax"),
    ):
        name = name.replace(fix_from, fix_to)
    return name or model_id


def is_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(s in lowered for s in SKIP_SUBSTRINGS)


def static_catalog() -> List[Dict[str, Any]]:
    """Catalog from the curated static table (order preserved)."""
    try:
        from nimcode.config import AVAILABLE_MODELS
    except Exception:
        return []
    catalog: List[Dict[str, Any]] = []
    for entry in AVAILABLE_MODELS.values():
        mid = entry.get("id", "")
        if not mid:
            continue
        catalog.append(
            {
                "id": mid,
                "name": entry.get("name") or pretty_name(mid),
                "context_tokens": MODEL_CONTEXT_TOKENS.get(mid),
            }
        )
    return catalog


def display_name(model_id: str) -> str:
    """Best display name for a model id (static table wins, else prettified)."""
    for entry in static_catalog():
        if entry.get("id") == model_id:
            return str(entry.get("name") or model_id)
    return pretty_name(model_id)


def fetch_models(client: Any, timeout: float = 20.0) -> List[Dict[str, Any]]:
    """Fetch the live catalog. Never raises — returns [] on any failure."""
    try:
        try:
            resp = client.models.list(timeout=timeout)
        except TypeError:
            resp = client.models.list()
        data = getattr(resp, "data", None) or []
    except Exception:
        return []
    static_names = {e.get("id"): e.get("name") for e in static_catalog() if e.get("id")}
    catalog: List[Dict[str, Any]] = []
    for m in data:
        try:
            if isinstance(m, dict):
                mid = str(m.get("id", ""))
                ctx = m.get("max_model_len")
            else:
                mid = str(getattr(m, "id", ""))
                ctx = getattr(m, "max_model_len", None)
        except Exception:
            continue
        if not mid or not is_chat_model(mid):
            continue
        try:
            ctx_int = int(ctx) if ctx else None
            if ctx_int is not None and ctx_int <= 0:
                ctx_int = None
        except (TypeError, ValueError):
            ctx_int = None
        if ctx_int is None:
            ctx_int = MODEL_CONTEXT_TOKENS.get(mid)
        catalog.append(
            {
                "id": mid,
                "name": static_names.get(mid) or pretty_name(mid),
                "context_tokens": ctx_int,
            }
        )
        if len(catalog) >= MAX_CATALOG_MODELS:
            break
    return catalog


NGC_CATALOG_URL = "https://api.ngc.nvidia.com/v2/search/catalog/resources/ENDPOINT"


def fetch_free_ids(timeout: float = 15.0) -> set:
    """Ids of hosted models NVIDIA labels "Free Endpoint" (public catalog).

    ``/v1/models`` carries no pricing, but build.nvidia.com's catalog marks
    free-tier endpoints. Never raises — returns an empty set on any failure.
    """
    try:
        import httpx

        free: set = set()
        page = 0
        while page < 10:
            query = {
                "query": "",
                "page": page,
                "pageSize": 100,
                "filters": [{"field": "nimType", "value": "nim_type_preview"}],
            }
            r = httpx.get(
                NGC_CATALOG_URL,
                params={"q": json.dumps(query)},
                timeout=timeout,
            )
            r.raise_for_status()
            body = r.json()
            for group in body.get("results", []):
                for res in group.get("resources", []):
                    labels = {
                        lab.get("key"): lab.get("values") or []
                        for lab in res.get("labels", [])
                    }
                    publisher = (labels.get("publisher") or [""])[0]
                    name = res.get("name")
                    if (
                        publisher
                        and name
                        and "Free Endpoint" in sum(labels.values(), [])
                    ):
                        free.add(f"{publisher}/{name}")
            page += 1
            if page >= int(body.get("resultPageTotal") or 1):
                break
        return free
    except Exception:
        return set()


def fetch_catalog(client: Any) -> List[Dict[str, Any]]:
    """Live models, each tagged ``free`` (kept from the cache if the lookup fails)."""
    fetched = fetch_models(client)
    if not fetched:
        return []
    free = fetch_free_ids()
    if not free:
        cached, _ = load_models_cache()
        free = {m["id"] for m in cached or [] if m.get("free")}
    for m in fetched:
        m["free"] = m["id"] in free
    return fetched


def load_models_cache() -> Tuple[Optional[List[Dict[str, Any]]], float]:
    """Return (models or None, age_seconds or inf)."""
    try:
        path = _cache_path()
        if not path.exists():
            return None, float("inf")
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return None, float("inf")
        models = payload.get("models")
        fetched_at = float(payload.get("fetched_at", 0))
        if not isinstance(models, list) or not fetched_at:
            return None, float("inf")
        clean = []
        for m in models:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                continue
            # Hand-edited/corrupt cache: non-int windows crash `{ctx:,}` callers.
            ctx = m.get("context_tokens")
            if ctx is not None and (
                not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0
            ):
                m = {**m, "context_tokens": None}
            if not isinstance(m.get("name"), str) or not m.get("name"):
                m = {**m, "name": pretty_name(m["id"])}  # callers index m["name"]
            clean.append(m)
        return clean, max(0.0, time.time() - fetched_at)
    except Exception:
        return None, float("inf")


def save_models_cache(models: List[Dict[str, Any]]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": time.time(), "models": models}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".models_tmp_")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass


def get_model_catalog() -> List[Dict[str, Any]]:
    """Single source of truth: fresh cache wins, else the static table."""
    cached, age = load_models_cache()
    if cached and age < _ttl_seconds():
        return cached
    return static_catalog()


def resolve_context(model_id: str) -> Optional[int]:
    """Native window for a model, or None when unknown (leave budget alone)."""
    if not model_id:
        return None
    return MODEL_CONTEXT_TOKENS.get(model_id)


def effective_budget(config: Any, model_id: str) -> int:
    """Prompt-token budget for pruning: never more than the model's real
    window, minus room for the reply (`max_tokens`) and a 1024 margin."""
    try:
        budget = int(getattr(config, "context_budget_tokens", DEFAULT_BUDGET))
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET
    window = resolve_context(model_id)
    if window:
        budget = min(budget, window)
    try:
        reply = int(getattr(config, "max_tokens", None) or 4096)
    except (TypeError, ValueError):
        reply = 4096
    return max(2000, budget - reply - 1024)


def apply_model_context(
    config: Any, model_id: str, force: bool = False
) -> Tuple[bool, str]:
    """Adjust ``config.context_budget_tokens`` to the model's window.

    Startup calls with ``force=False`` (only pristine-default budgets move, so
    user customizations survive restarts). Explicit model switches call with
    ``force=True``. Returns (changed, human-readable message).
    """
    window = resolve_context(model_id)
    if not window:
        return False, f"Context window unknown for {model_id}; budget unchanged."
    try:
        current = int(getattr(config, "context_budget_tokens", DEFAULT_BUDGET))
    except (TypeError, ValueError):
        current = DEFAULT_BUDGET
    if not force and current != DEFAULT_BUDGET:
        return False, "Custom context budget kept."
    if current == window:
        return False, f"Context budget already {window:,}."
    config.context_budget_tokens = window
    try:
        if int(getattr(config, "max_tokens", 4096)) >= window:
            config.max_tokens = max(1024, window // 4)
    except (TypeError, ValueError):
        pass
    return True, f"Context budget → {window:,} tokens ({model_id})."


def reconcile_saved_model(
    config: Any, catalog: Optional[List[Dict[str, Any]]] = None
) -> Optional[str]:
    """Auto-fallback for retired saved models. Returns a notice or None."""
    catalog = catalog if catalog is not None else get_model_catalog()
    ids = {m.get("id") for m in catalog if m.get("id")}
    if not ids:
        return None
    # The static table is a curated subset, not the live catalog: any valid
    # model set in nimcode.toml would be "retired" and overwritten on disk.
    if ids == {m["id"] for m in static_catalog()}:
        return None
    notices: List[str] = []
    for attr in ("fast_model", "plan_model"):
        current = getattr(config, attr, None)
        if current and current not in ids:
            fallback = DEFAULT_MODEL_ID if DEFAULT_MODEL_ID in ids else sorted(ids)[0]
            setattr(config, attr, fallback)
            notices.append(f"Model {current} retired; switched {attr} to {fallback}.")
    if notices:
        try:
            from nimcode.config import save_last_used_model

            save_last_used_model(config, config.fast_model, None)
        except Exception:
            pass
        return " ".join(notices)
    return None


def refresh_models_blocking(client: Any) -> Tuple[List[Dict[str, Any]], str]:
    """Blocking refresh: fetch → cache → catalog. Returns (catalog, message)."""
    global _refresh_inflight
    with _refresh_lock:
        if _refresh_inflight:
            return get_model_catalog(), "Refresh already in progress."
        _refresh_inflight = True
    try:
        fetched = fetch_catalog(client)
        if not fetched:
            return (
                get_model_catalog(),
                "Live fetch failed; kept existing catalog.",
            )
        save_models_cache(fetched)
        return fetched, f"Catalog refreshed: {len(fetched)} models."
    finally:
        with _refresh_lock:
            _refresh_inflight = False


def refresh_models_background(client: Any) -> None:
    """Best-effort background refresh. Never raises, never blocks startup."""
    if refresh_disabled():
        return

    def _worker() -> None:
        try:
            fetched = fetch_catalog(client)
            if fetched:
                save_models_cache(fetched)
        except Exception:
            pass

    try:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
    except Exception:
        pass
