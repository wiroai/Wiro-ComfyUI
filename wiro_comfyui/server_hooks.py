"""Hooks into the ComfyUI server: the prompt scrubber and the route the
browser extension uses to hand over a key. See keystore.py."""

from __future__ import annotations

import logging

from . import keystore

log = logging.getLogger("comfyui-wiro")
_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return  # not running inside ComfyUI (tests, scripts)
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return

    instance.add_on_prompt_handler(keystore.scrub_prompt)

    @instance.routes.post("/wiro/secret")
    async def register_secret(request):
        try:
            body = await request.json()
        except Exception:
            body = None
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value.strip():
            return web.json_response({"error": "value is required"}, status=400)
        try:
            ref = keystore.put(value)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ref": ref})

    @instance.routes.get("/wiro/prices")
    async def prices(request):
        """Price tables for the browser extension's price display (public catalog data)."""
        from . import catalog
        from .nodes import FIXED_NODE_MODELS

        return web.json_response(price_tables(catalog.models(), catalog.llm_models(), FIXED_NODE_MODELS))

    _registered = True


def price_tables(models: list, llm_models: list, fixed: dict) -> dict:
    return {
        "models": {
            m["slug"]: {"price": m.get("price") or [], "cps": m.get("cps"), "approx": m.get("approx")}
            for m in models
        },
        "llm": {m["id"]: {"prompt": m.get("prompt"), "completion": m.get("completion")} for m in llm_models},
        "fixed": dict(fixed),
    }
