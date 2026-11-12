"""Which model parameter each category node input goes to.

Category nodes keep the inputs every model in the category shares (prompt,
reference images, mask, first and last frame, audio, video) at the top of the
node, so links and text survive a model change. This module finds the
matching parameter of each model and lists the parameters left over for the
model's own settings.
"""

from __future__ import annotations

from typing import Optional

FILE_TYPES = ("fileinput", "multifileinput", "combinefileinput")
TEXT_TYPES = ("text", "textarea")

# Category nodes: catalog categories, node inputs (True = the model must
# have a matching parameter to be listed), and what the node returns.
CATEGORY_SPECS = [
    {
        "node": "WiroTextToImage", "display": "Wiro Text to Image", "menu": "Image",
        "categories": ["text-to-image"], "roles": {"text": True}, "kind": "image",
        "default": "google/nano-banana",
        "description": "Generates images from a prompt with any text-to-image model in the Wiro AI catalog.",
    },
    {
        "node": "WiroImageEdit", "display": "Wiro Image Edit", "menu": "Image",
        "categories": ["image-editing"], "roles": {"text": True, "images": True, "mask": False}, "kind": "image",
        "default": "google/nano-banana",
        "description": "Edits images from a prompt, with reference images and an optional mask, using any image editing model in the Wiro AI catalog.",
    },
    {
        "node": "WiroUpscaleImage", "display": "Wiro Upscale Image", "menu": "Image",
        "categories": ["upscale"], "roles": {"image": True}, "kind": "image",
        "default": "wiroai/flux-upscaler",
        "description": "Upscales and restores an image with the upscalers in the Wiro AI catalog.",
    },
    {
        "node": "WiroRemoveBackground", "display": "Wiro Remove Background", "menu": "Image",
        "categories": ["background-removal"], "roles": {"image": True}, "kind": "image_mask",
        "default": "wiro/remove-background",
        "description": "Removes the background of an image. Returns the cut-out image and its alpha mask.",
    },
    {
        "node": "WiroTextToVideo", "display": "Wiro Text to Video", "menu": "Video",
        "categories": ["text-to-video"], "roles": {"text": True}, "kind": "video",
        "default": "bytedance/seedance-v1-pro-fast",
        "description": "Generates a video from a prompt with any text-to-video model in the Wiro AI catalog.",
    },
    {
        "node": "WiroImageToVideo", "display": "Wiro Image to Video", "menu": "Video",
        "categories": ["image-to-video"], "roles": {"text": False, "first_frame": True, "last_frame": False},
        "kind": "video", "default": "bytedance/seedance-v1-pro-fast",
        "description": "Animates an image into a video, with an optional last frame, using any image-to-video model in the Wiro AI catalog.",
    },
    {
        "node": "WiroVideoUpscale", "display": "Wiro Video Upscale", "menu": "Video",
        "categories": ["upscale"], "roles": {"video": True}, "kind": "video",
        "default": "pruna/p-video-upscale",
        "description": "Upscales a video with the video upscalers in the Wiro AI catalog.",
    },
    {
        "node": "WiroLipsync", "display": "Wiro Lipsync", "menu": "Video",
        "categories": ["talking-head", "speech-to-video"], "roles": {"image": False, "video": False, "audio": True},
        "kind": "video", "default": "pruna/p-video-avatar",
        "description": "Makes a portrait image or video speak along with an audio track, using the talking-head and lipsync models in the Wiro AI catalog.",
    },
    {
        "node": "WiroTextToSpeech", "display": "Wiro Text to Speech", "menu": "Audio",
        "categories": ["text-to-speech"], "roles": {"text": True}, "kind": "audio",
        "default": "elevenlabs/eleven-v4", "text_input": "text",
        "description": "Turns text into speech with any text-to-speech model in the Wiro AI catalog; each model brings its own voices.",
    },
    {
        "node": "WiroMusic", "display": "Wiro Music", "menu": "Audio",
        "categories": ["music-generation", "text-to-music", "text-to-song"], "roles": {"text": False}, "kind": "audio",
        "default": "elevenlabs/text-to-music-v2",
        "description": "Generates music or songs from a prompt with the music models in the Wiro AI catalog.",
    },
    {
        "node": "WiroSpeechToText", "display": "Wiro Speech to Text", "menu": "Audio",
        "categories": ["speech-to-text"], "roles": {"audio": True}, "kind": "text",
        "default": "elevenlabs/speech-to-text",
        "description": "Transcribes speech to text with the speech-to-text models in the Wiro AI catalog.",
    },
    {
        "node": "Wiro3D", "display": "Wiro 3D", "menu": "3D",
        "categories": ["3d-generation"], "roles": {"text": False, "image": False}, "kind": "3d",
        "default": "microsoft/trellis-2",
        "description": "Generates a 3D model (GLB) from text or an image with the 3D models in the Wiro AI catalog.",
    },
]

ROLE_SOCKETS = {
    "images": "IMAGE", "image": "IMAGE", "mask": "IMAGE",
    "first_frame": "IMAGE", "last_frame": "IMAGE", "video": "VIDEO", "audio": "AUDIO",
}


def file_kind(param: dict) -> Optional[str]:
    """image, audio, video or None for a file parameter."""
    pid = param["id"].lower()
    label = (param.get("label") or "").lower()
    text = f"{pid} {label}"
    if "audio" in text or "voice" in pid or "speech" in pid:
        return "audio"
    if "video" in text:
        return "video"
    if any(word in text for word in ("image", "photo", "picture", "frame", "face", "mask", "portrait", "selfie")):
        return "image"
    return None


def _is_mask(param: dict) -> bool:
    return "mask" in f"{param['id']} {param.get('label') or ''}".lower()


def _is_last_frame(param: dict) -> bool:
    text = f"{param['id']} {param.get('label') or ''}".lower()
    return any(word in text for word in ("last", "end frame", "endframe", "tail", "final frame"))


def text_param(params: list) -> Optional[dict]:
    by_id = {p["id"]: p for p in params if p["type"] in TEXT_TYPES}
    for name in ("prompt", "text", "inputText", "script"):
        if name in by_id:
            return by_id[name]
    for p in params:
        pid = p["id"].lower()
        if p["type"] == "textarea" and not any(w in pid for w in ("negative", "system", "lyric", "json", "multi")):
            return p
    return None


def find_roles(params: list) -> dict:
    """Role -> parameter for every role this model's parameters can fill."""
    roles: dict = {}
    text = text_param(params)
    if text:
        roles["text"] = text
    files = [p for p in params if p["type"] in FILE_TYPES]
    images = [p for p in files if file_kind(p) == "image"]
    plain = [p for p in images if not _is_mask(p)]
    masks = [p for p in images if _is_mask(p)]
    lasts = [p for p in plain if _is_last_frame(p)]
    firsts = [p for p in plain if not _is_last_frame(p)]
    if firsts:
        roles["images"] = roles["image"] = roles["first_frame"] = firsts[0]
    if lasts:
        roles["last_frame"] = lasts[0]
    if masks:
        roles["mask"] = masks[0]
    for kind in ("audio", "video"):
        found = [p for p in files if file_kind(p) == kind]
        if found:
            roles[kind] = found[0]
    return roles


def plan_model(spec: dict, model: dict) -> Optional[dict]:
    """How a model fits a category node, or None if it cannot run from it.

    Returns {"roles": role -> param, "settings": other non-file params,
    "extra_files": file params left for the model's own inputs}.
    """
    params = model.get("params") or []
    available = find_roles(params)
    roles = {}
    for role, required in spec["roles"].items():
        if role in available:
            roles[role] = available[role]
        elif required:
            return None
    used = {p["id"] for p in roles.values()}
    extra_files = []
    for p in params:
        if p["type"] not in FILE_TYPES or p["id"] in used:
            continue
        kind = file_kind(p)
        if kind is None:
            if p.get("required"):
                return None  # a document or other file this node cannot provide
            continue
        extra_files.append(p)  # becomes an extra IMAGE, AUDIO or VIDEO input of this model
    settings = [p for p in params if p["type"] not in FILE_TYPES and p["id"] not in used]
    return {"roles": roles, "settings": settings, "extra_files": extra_files}


def models_for(spec: dict, catalog: list) -> list:
    """(model, plan) pairs for a category node: the default model first,
    then the rest by how often they run."""
    wanted = set(spec["categories"])
    picked = []
    for model in catalog:
        if not wanted.intersection(model.get("categories") or []):
            continue
        plan = plan_model(spec, model)
        if plan is not None:
            picked.append((model, plan))
    picked.sort(key=lambda mp: (mp[0]["slug"] != spec.get("default"), -mp[0].get("runs", 0), mp[0]["slug"]))
    return picked
