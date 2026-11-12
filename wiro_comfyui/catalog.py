"""The Wiro AI model catalog behind the model menus.

At startup the nodes read the last saved copy of the catalog (or the copy
shipped with the pack), so ComfyUI starts at once even offline. A background
thread then fetches the live catalog from the public Tool/List endpoint and
saves it; ComfyUI rebuilds node menus whenever the browser loads, so new
models show up after a page reload, with no update of the pack.

The LLM gateway's model list (https://llm.wiro.ai/v1/models) is handled the
same way.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from typing import Any, Optional

import requests

from .client import API_BASE, USER_AGENT

log = logging.getLogger("comfyui-wiro")

LLM_MODELS_URL = "https://llm.wiro.ai/v1/models?limit=1000"
REFRESH_AFTER = 6 * 3600  # seconds before a saved copy is fetched again
CACHE_FORMAT = 2  # bump when the saved record shape changes; older files are ignored
CATALOG_CACHE = f"catalog-v{CACHE_FORMAT}.json"
LLM_CACHE = f"llm-models-v{CACHE_FORMAT}.json"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(PACKAGE_DIR, "catalog_snapshot.json.gz")
LLM_SNAPSHOT = os.path.join(PACKAGE_DIR, "llm_models_snapshot.json")

# Models that stream over a WebSocket session cannot run as a single task.
SKIP_CATEGORIES = {"realtime-conversation", "realtime-tts", "realtime-stt", "voice-agent"}
KEEP_TYPES = {
    "text", "textarea", "select", "selectwithcover", "number", "float", "range", "checkbox",
    "json", "json-array", "fileinput", "multifileinput", "combinefileinput",
}
FILE_TYPES = ("fileinput", "multifileinput", "combinefileinput")
NOTE_LIMIT = 300

_lock = threading.Lock()
_models: list = []
_by_slug: dict = {}
_llm_models: list = []
_version = 0
_started = False


# ---------------------------------------------------------------- shaping


def _clean_text(text: Any) -> str:
    text = " ".join(str(text or "").split())
    em_dash, en_dash = chr(0x2014), chr(0x2013)
    return text.replace(f" {em_dash} ", ", ").replace(em_dash, "-").replace(en_dash, "-")


def _number(value: Any):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def compact_param(item: dict) -> Optional[dict]:
    """The fields of one Tool/Detail parameter the nodes use."""
    pid, ptype = item.get("id"), item.get("type")
    if not pid or ptype not in KEEP_TYPES:
        return None
    entry = {
        "id": str(pid),
        "type": "select" if ptype == "selectwithcover" else str(ptype),
        "label": _clean_text(item.get("label") or pid),
        "note": _clean_text(item.get("note"))[:NOTE_LIMIT],
        "required": bool(item.get("required")),
        "advanced": bool(item.get("advanced")),
    }
    if ptype in FILE_TYPES:
        max_files = _number(item.get("maxinputlenght"))
        entry["max_files"] = max_files if isinstance(max_files, int) and max_files > 0 else None
        return entry
    if ptype in ("text", "textarea", "json", "json-array"):
        entry["default"] = ""  # Wiro's text defaults are sample prompts; the nodes start empty
    else:
        entry["default"] = "" if item.get("defaultvalue") is None else str(item.get("defaultvalue"))
    if ptype in ("select", "selectwithcover"):
        entry["options"] = [str(o.get("value", "")) for o in item.get("options") or [] if isinstance(o, dict)]
    if ptype in ("number", "float", "range"):
        entry["min"] = _number(item.get("minvalue"))
        entry["max"] = _number(item.get("maxvalue"))
        entry["step"] = _number(item.get("incrementby"))
    return entry


def _price_list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    prices = []
    for item in value:
        if isinstance(item, dict) and "price" in item:
            prices.append({
                "inputs": item.get("inputs") if isinstance(item.get("inputs"), dict) else {},
                "price": _number(item.get("price")) or 0,
                "method": str(item.get("priceMethod") or ""),
                **({"note": _clean_text(item.get("note"))[:200]} if item.get("note") else {}),
                **({"extra": _number(item.get("priceExtra"))} if item.get("priceExtra") is not None else {}),
                **({"input": _number(item.get("priceInput"))} if item.get("priceInput") is not None else {}),
            })
    return prices


def compact_model(tool: dict) -> Optional[dict]:
    owner, project = tool.get("cleanslugowner"), tool.get("cleanslugproject")
    if not owner or not project:
        return None
    categories = [str(c).strip() for c in tool.get("categories") or [] if str(c).strip()]
    if SKIP_CATEGORIES.intersection(categories):
        return None
    params = []
    for group in tool.get("parameters") or []:
        for item in (group or {}).get("items") or []:
            entry = compact_param(item)
            if entry is not None:
                params.append(entry)
    stats = tool.get("taskstat") or {}
    approx = _number(tool.get("approximatelycost"))
    return {
        "slug": f"{owner}/{project}",
        "title": _clean_text(tool.get("title")),
        "categories": categories,
        "runs": int(_number(stats.get("runcount")) or 0),
        # GPU-time models: price per second, and Wiro's typical cost per run
        "cps": _number(tool.get("cps")) or _number(tool.get("quegroupcps")) or None,
        "approx": round(approx, 6) if approx and approx > 0 else None,
        "price": _price_list(tool.get("dynamicprice")),
        "params": params,
    }


def compact_llm_model(model: dict) -> Optional[dict]:
    model_id = model.get("id")
    if not model_id:
        return None
    caps = model.get("capabilities") or {}
    modalities = caps.get("input_modalities") or (model.get("architecture") or {}).get("input_modalities") or []
    pricing = model.get("pricing") or {}
    return {
        "id": str(model_id),
        "name": _clean_text(model.get("display_name") or model.get("name") or model_id),
        "image": "image" in modalities,
        "endpoints": list(caps.get("endpoints") or []),
        "prompt": _number(pricing.get("prompt")),
        "completion": _number(pricing.get("completion")),
        "max_tokens": _number(model.get("max_tokens")),
    }


# ---------------------------------------------------------------- fetching


def fetch_catalog(timeout: float = 60) -> list:
    """All public models, with their parameters and prices, from Tool/List."""
    models, start, limit = [], 0, 500
    while True:
        response = requests.post(
            f"{API_BASE}/Tool/List",
            json={"start": str(start), "limit": str(limit), "hideworkflows": True, "summary": False},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("result") is not True:
            raise RuntimeError(f"Tool/List failed: {body.get('errors')}")
        tools = body.get("tool") or []
        models += [m for m in (compact_model(t) for t in tools) if m]
        if len(tools) < limit:
            return models
        start += limit


def fetch_llm_models(timeout: float = 30) -> list:
    response = requests.get(LLM_MODELS_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    data = response.json().get("data") or []
    models = [m for m in (compact_llm_model(x) for x in data) if m]
    return [m for m in models if "chat" in m["endpoints"]]


# ---------------------------------------------------------------- storage


def _cache_dir() -> Optional[str]:
    try:
        import folder_paths

        path = os.path.join(folder_paths.get_user_directory(), "comfyui-wiro")
    except Exception:
        return None
    os.makedirs(path, exist_ok=True)
    return path


def _read_json(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def _load(name: str, snapshot: str) -> tuple:
    """(data, age in seconds or None for the shipped snapshot)."""
    folder = _cache_dir()
    if folder:
        path = os.path.join(folder, name)
        try:
            data = _read_json(path)
            if isinstance(data, list) and data:
                return data, time.time() - os.path.getmtime(path)
        except (OSError, ValueError):
            pass
    try:
        return _read_json(snapshot), None
    except (OSError, ValueError):
        return [], None


def _set_models(models: list) -> None:
    global _models, _by_slug, _version
    with _lock:
        _models = models
        _by_slug = {m["slug"]: m for m in models}
        _version += 1


def _set_llm_models(models: list) -> None:
    global _llm_models, _version
    with _lock:
        _llm_models = models
        _version += 1


def _refresh(kind: str) -> None:
    folder = _cache_dir()
    try:
        if kind == "catalog":
            models = fetch_catalog()
            _set_models(models)
            name = CATALOG_CACHE
        else:
            models = fetch_llm_models()
            _set_llm_models(models)
            name = LLM_CACHE
        if folder:
            _write_json(os.path.join(folder, name), models)
        log.info("[Wiro] %s refreshed: %d models", kind, len(models))
    except Exception as exc:  # network trouble must never break ComfyUI
        log.warning("[Wiro] could not refresh the %s: %s", kind, exc)


def start() -> None:
    """Load the saved catalogs and refresh old ones in the background. Idempotent."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    models, age = _load(CATALOG_CACHE, SNAPSHOT)
    _set_models(models)
    llm, llm_age = _load(LLM_CACHE, LLM_SNAPSHOT)
    _set_llm_models(llm)
    stale = [k for k, a in (("catalog", age), ("llm models", llm_age)) if a is None or a > REFRESH_AFTER]
    if stale:
        def run():
            for kind in stale:
                _refresh("catalog" if kind == "catalog" else "llm")
        threading.Thread(target=run, name="wiro-catalog-refresh", daemon=True).start()


def models() -> list:
    with _lock:
        return list(_models)


def model(slug: str) -> Optional[dict]:
    with _lock:
        return _by_slug.get(slug)


def llm_models() -> list:
    with _lock:
        return list(_llm_models)


def version() -> int:
    with _lock:
        return _version


def set_for_tests(catalog: list, llm: Optional[list] = None) -> None:
    _set_models(catalog)
    _set_llm_models(llm or [])
