"""Shared rules for turning Wiro parameters into widgets and back."""

from __future__ import annotations

import re
from typing import Any, Optional

FILE_TYPES = ("fileinput", "multifileinput", "combinefileinput")
BOOLEAN_WORDS = ("", "true", "false", "1", "0", "on", "off", "yes", "no")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def display_name(param: dict) -> Optional[str]:
    """Lowercase label for a widget, from Wiro's label or the parameter id.

    Some Wiro labels are ids or translation keys ("outputFormat",
    "input-audio"); those are rebuilt from the id ("output format").
    Returns None when the label would just repeat the id.
    """
    pid = param["id"]
    label = (param.get("label") or "").strip()
    if not label or (" " not in label and ("-" in label or label[0].islower())):
        label = _CAMEL_BOUNDARY.sub(" ", pid)
    label = " ".join(label.lower().split())
    return None if label == pid.lower() else label


def is_int(*values) -> bool:
    return all(v is None or (isinstance(v, (int, float)) and float(v).is_integer()) for v in values)


def select_choices(param: dict) -> tuple:
    """(options, default) for a select parameter."""
    choices = list(param.get("options") or [])
    default = param.get("default", "")
    if default not in choices:
        if not param.get("required") and "" not in choices:
            choices.insert(0, "")
        default = "" if "" in choices else (choices[0] if choices else "")
    if not choices:
        choices = [""]
    return choices, default


def number_spec(param: dict) -> Optional[dict]:
    """INT or FLOAT widget settings for a number parameter, or None when it
    has no usable default (such parameters are left to the model)."""
    ptype = param["type"]
    low, high, step = param.get("min"), param.get("max"), param.get("step")
    raw = param.get("default", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if param.get("required") and low is not None:
            value = float(low)
        else:
            return None
    if ptype != "float" and is_int(low, high, step, value):
        spec = {"kind": "INT", "default": int(value), "step": int(step or 1)}
        if low is not None:
            spec["min"] = int(low)
        if high is not None:
            spec["max"] = int(high)
        if spec.get("min") is not None and spec["default"] < spec["min"]:
            spec["default"] = spec["min"]
        if spec.get("max") is not None and spec["default"] > spec["max"]:
            spec["default"] = spec["max"]
        return spec
    spec = {"kind": "FLOAT", "default": value, "step": float(step or 0.01)}
    if low is not None:
        spec["min"] = float(low)
    if high is not None:
        spec["max"] = float(high)
    return spec


def wiro_value(param: dict, value: Any):
    """Value to send for a widget, or None to leave the parameter out."""
    ptype = param["type"]
    if ptype == "checkbox":
        default = str(param.get("default", "")).strip()
        if default.lower() in BOOLEAN_WORDS:
            return "true" if value else "false"
        # A checkbox with its own value (such as "--face_enhance") sends it when ticked.
        return default if value else None
    if ptype in ("number", "float", "range"):
        if param["id"] == "seed" and not value:
            return None
        return value
    if isinstance(value, str) and value == "" and not param.get("required"):
        return None
    if ptype in ("json", "json-array") and isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return text
    return value
