"""Refresh wiro_comfyui/models.json from the public Wiro AI catalog.

The model nodes are built from this file when ComfyUI starts, so ComfyUI
never calls the network to show them. Run this script after a model on Wiro
gains or changes parameters, then check the diff:

    python scripts/update_models.py

Tool/Detail is a public endpoint, so no API key is needed.
"""

from __future__ import annotations

import json
import os
import sys

import requests

API = "https://api.wiro.ai/v1/Tool/Detail"
OUT = os.path.join(os.path.dirname(__file__), "..", "wiro_comfyui", "models.json")

# node class, display name, model, kind, parameters left out, description
MODELS = [
    (
        "WiroNanoBananaPro", "Wiro Nano Banana Pro", "google/nano-banana-pro", "image", [],
        "Google Nano Banana Pro (Gemini 3 Pro Image): creates and edits images at 1K, 2K or 4K "
        "from a prompt and up to 14 reference images.",
    ),
    (
        "WiroNanoBanana2", "Wiro Nano Banana 2", "google/nano-banana-2", "image", [],
        "Google Nano Banana 2 (Gemini 3.1 Flash Image): image generation and editing with up to "
        "14 reference images, from 512 px to 4K.",
    ),
    (
        "WiroGPTImage2", "Wiro GPT Image 2", "openai/gpt-image-2", "image", [],
        "OpenAI GPT Image 2: generates images, or edits up to 16 input images, with an optional mask.",
    ),
    (
        "WiroSeedream45", "Wiro Seedream 4.5", "bytedance/seedream-v4-5", "image", [],
        "ByteDance Seedream 4.5: image generation and editing from a prompt and up to 14 "
        "reference images, at 2K or 4K.",
    ),
    (
        "WiroVeo31", "Wiro Veo 3.1", "google/veo3-1", "video", [],
        "Google Veo 3.1: text to video, image to video from a first and last frame, or video "
        "from up to 3 reference images.",
    ),
    (
        "WiroKling3", "Wiro Kling 3", "klingai/kling-v3", "video", ["multiShot", "shotType", "multiPrompt"],
        "Kling 3.0: 5, 10 or 15 second videos from a prompt or a first and last frame, with "
        "optional sound.",
    ),
    (
        "WiroSeedance1ProFast", "Wiro Seedance 1 Pro Fast", "bytedance/seedance-v1-pro-fast", "video", [],
        "ByteDance Seedance 1 Pro Fast: text to video or image to video, 5 or 10 seconds, "
        "480p to 1080p.",
    ),
    (
        "WiroElevenV4", "Wiro Eleven v4 Text to Speech", "elevenlabs/eleven-v4", "audio",
        ["timestamps", "seed"],
        "ElevenLabs Eleven v4: speech from text, with audio tags like [whispering] or [laughing].",
    ),
    (
        "WiroElevenMusic2", "Wiro ElevenLabs Music v2", "elevenlabs/text-to-music-v2", "audio", [],
        "ElevenLabs Music v2: a music track from a text brief, 10 to 70 seconds, with or "
        "without vocals.",
    ),
    (
        "WiroElevenSpeechToText", "Wiro ElevenLabs Speech to Text", "elevenlabs/speech-to-text", "text",
        ["inputAudioUrl"],
        "ElevenLabs Scribe: transcribes speech to punctuated text and detects the language.",
    ),
]

KEEP_TYPES = {
    "text", "textarea", "select", "number", "float", "range", "checkbox",
    "fileinput", "multifileinput", "combinefileinput",
}


def clean_text(text: str) -> str:
    """Wiro's labels and notes become tooltips; keep them plain."""
    text = " ".join(str(text or "").split())
    em_dash, en_dash = chr(0x2014), chr(0x2013)
    return text.replace(f" {em_dash} ", ", ").replace(em_dash, "-").replace(en_dash, "-")


def number_or_none(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def fetch(model: str) -> dict:
    owner, project = model.split("/")
    response = requests.post(API, json={"slugowner": owner, "slugproject": project}, timeout=30)
    response.raise_for_status()
    body = response.json()
    if body.get("result") is not True or not body.get("tool"):
        raise SystemExit(f"{model}: {body.get('errors')}")
    return body["tool"][0]


def parameters(tool: dict, excluded: list) -> list:
    result = []
    for group in tool.get("parameters") or []:
        for item in group.get("items") or []:
            pid, ptype = item.get("id"), item.get("type")
            if not pid or pid in excluded or ptype not in KEEP_TYPES:
                continue
            entry = {
                "id": pid,
                "type": ptype,
                "label": clean_text(item.get("label") or pid),
                "note": clean_text(item.get("note")),
                "required": bool(item.get("required")),
                "advanced": bool(item.get("advanced")),
            }
            if ptype in ("fileinput", "multifileinput", "combinefileinput"):
                max_files = number_or_none(item.get("maxinputlenght"))
                entry["max_files"] = max_files if isinstance(max_files, int) and max_files > 0 else None
            else:
                entry["default"] = "" if item.get("defaultvalue") is None else str(item.get("defaultvalue"))
                if ptype == "select":
                    entry["options"] = [str(o.get("value", "")) for o in item.get("options") or []]
                if ptype in ("number", "float", "range"):
                    entry["min"] = number_or_none(item.get("minvalue"))
                    entry["max"] = number_or_none(item.get("maxvalue"))
                    entry["step"] = number_or_none(item.get("incrementby"))
            result.append(entry)
    return result


def main() -> int:
    models = []
    for node, display, model, kind, excluded, description in MODELS:
        tool = fetch(model)
        models.append({
            "node": node,
            "display_name": display,
            "model": model,
            "kind": kind,
            "description": description,
            "parameters": parameters(tool, excluded),
        })
        print(f"{model}: {len(models[-1]['parameters'])} parameters")
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump({"models": models}, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"wrote {os.path.relpath(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
