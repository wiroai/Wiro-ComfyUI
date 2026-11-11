"""Keeps keys typed into the Wiro API Key node out of everything ComfyUI saves.

ComfyUI stores the prompt it runs in its history and in the metadata of saved
images and videos, and the browser saves the workflow it shows. A key typed
into a widget would end up in all of them. So:

- the browser extension (web/wiro.js) hands the key to this server process and
  sends only a reference ("wiro-secret:...") in the prompt, and it saves the
  widget empty in workflow files;
- scrub_prompt(), registered as a ComfyUI on_prompt handler, replaces any raw
  key that still arrives in a prompt (an API client, an old browser tab) with
  a reference before ComfyUI validates or stores the prompt, and empties the
  widget in the workflow copy that goes into file metadata.

References only resolve inside the ComfyUI process that issued them. They are
an HMAC of the value under a random per-process salt, so the same key always
gives the same reference (ComfyUI's cache keeps working) and a reference says
nothing about the key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
from collections import OrderedDict
from typing import Any, Optional

REF_PREFIX = "wiro-secret:"
KEY_NODE_CLASS = "WiroApiKey"
SECRET_INPUTS = ("api_key", "api_secret")
MAX_SECRET_LENGTH = 1024

_SALT = os.urandom(32)
_MAX_ENTRIES = 64
_lock = threading.Lock()
_store: "OrderedDict[str, str]" = OrderedDict()


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REF_PREFIX)


def put(value: str) -> str:
    """Store a secret in memory and return its reference."""
    value = (value or "").strip()
    if not value:
        return ""
    if is_ref(value):
        return value
    if len(value) > MAX_SECRET_LENGTH:
        raise ValueError("The value is too long to be a Wiro API key or secret.")
    digest = hmac.new(_SALT, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    ref = REF_PREFIX + digest
    with _lock:
        _store[ref] = value
        _store.move_to_end(ref)
        while len(_store) > _MAX_ENTRIES:
            _store.popitem(last=False)
    return ref


def resolve(value: Any) -> Optional[str]:
    """The secret behind a reference, the value itself if it is not one,
    or None for a reference this process does not know."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not is_ref(value):
        return value
    with _lock:
        return _store.get(value)


def _scrub_workflow(node: Any) -> None:
    """Empty the secret widgets of Wiro API Key nodes in a workflow (also inside subgraphs)."""
    if isinstance(node, dict):
        if node.get("type") == KEY_NODE_CLASS:
            if isinstance(node.get("widgets_values"), list):
                node["widgets_values"] = [
                    "" if isinstance(v, str) else v for v in node["widgets_values"]
                ]
            named = node.get("widgets_values_named")  # newer frontends save values by name too
            if isinstance(named, dict):
                for name, value in named.items():
                    if isinstance(value, str):
                        named[name] = ""
        for value in node.values():
            if isinstance(value, (dict, list)):
                _scrub_workflow(value)
    elif isinstance(node, list):
        for value in node:
            _scrub_workflow(value)


def scrub_prompt(json_data: Any) -> Any:
    """ComfyUI on_prompt handler. Must never raise and must return json_data."""
    if not isinstance(json_data, dict):
        return json_data
    prompt = json_data.get("prompt")
    if isinstance(prompt, dict):
        for node in prompt.values():
            if not isinstance(node, dict) or node.get("class_type") != KEY_NODE_CLASS:
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for name in SECRET_INPUTS:
                value = inputs.get(name)
                if isinstance(value, str) and value.strip() and not is_ref(value):
                    try:
                        inputs[name] = put(value)
                    except ValueError:
                        inputs[name] = ""
    extra = json_data.get("extra_data")
    if isinstance(extra, dict):
        pnginfo = extra.get("extra_pnginfo")
        if isinstance(pnginfo, dict) and "workflow" in pnginfo:
            _scrub_workflow(pnginfo["workflow"])
    return json_data
