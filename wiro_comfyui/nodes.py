"""ComfyUI node classes: Wiro API Key, Wiro Run Model and one node per model
in models.json."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import keystore, runner
from .client import (
    MISSING_KEY_MESSAGE,
    WiroClient,
    WiroCredentials,
    WiroError,
    credentials_from_env,
    parse_model,
    resolve_credentials,
)
from .payload import parse_parameters, pick_file_field

CATEGORY = "Wiro AI"
AUTH_TYPE = "WIRO_AUTH"

AUTH_INPUT = (
    AUTH_TYPE,
    {"tooltip": "Optional. Connect a Wiro API Key node. Without it the WIRO_API_KEY environment variable is used."},
)
TIMEOUT_INPUT = (
    "INT",
    {
        "default": 1800, "min": 30, "max": 7200, "step": 30, "advanced": True,
        "tooltip": "Seconds to wait for the task. A task that takes longer keeps running on Wiro.",
    },
)
FORCE_RERUN_INPUT = (
    "BOOLEAN",
    {
        "default": False, "advanced": True,
        "tooltip": "Run again on every queue, even when no input changed. Every run is billed.",
    },
)


class WiroApiKey:
    """Holds a Wiro API key for the other Wiro nodes."""

    DESCRIPTION = (
        "Your Wiro AI API key, for the Wiro nodes this connects to. The key is never "
        "written into saved workflows, image or video metadata, or ComfyUI's history, "
        "so type it again after you reload a workflow or restart ComfyUI. Leave it "
        "empty to use the WIRO_API_KEY environment variable."
    )
    CATEGORY = CATEGORY
    FUNCTION = "make"
    RETURN_TYPES = (AUTH_TYPE,)
    RETURN_NAMES = ("wiro_auth",)
    OUTPUT_TOOLTIPS = ("Connect to the wiro_auth input of any Wiro node.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {
                    "default": "",
                    "tooltip": "API key of a Wiro AI project (wiro.ai/panel/project). Not saved with the workflow.",
                }),
            },
            "optional": {
                "api_secret": ("STRING", {
                    "default": "",
                    "tooltip": "Only for projects that use signature authentication. Not saved with the workflow.",
                }),
            },
        }

    def make(self, api_key: str = "", api_secret: str = ""):
        key = keystore.resolve(api_key or "")
        secret = keystore.resolve(api_secret or "")
        if key is None or secret is None:
            raise WiroError(
                "The key in the Wiro API Key node is no longer available. Keys are never saved "
                "with workflows, and ComfyUI only keeps them until it restarts. Type the key "
                "again, or set the WIRO_API_KEY environment variable."
            )
        if not key:
            env = credentials_from_env()
            if env is None:
                raise WiroError(MISSING_KEY_MESSAGE)
            return (env,)
        return (WiroCredentials(key, secret),)


DEFAULT_PARAMETERS = '{\n  "resolution": "1k",\n  "ratio": "1:1",\n  "quality": "low"\n}'


class WiroRunModel:
    """Runs any Wiro AI model by its owner/project slug."""

    DESCRIPTION = (
        "Runs any model from the Wiro AI catalog (wiro.ai/models): type its owner/project "
        "slug, a prompt, and the other parameters as JSON. Connected images, audio and video "
        "are uploaded to the model's file inputs. Returns what the model made as IMAGE, "
        "VIDEO, AUDIO and text, plus the result URLs. Each run is billed to your Wiro AI "
        "account at the model's price."
    )
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "VIDEO", "AUDIO", "STRING", "STRING")
    RETURN_NAMES = ("images", "video", "audio", "text", "urls")
    OUTPUT_TOOLTIPS = (
        "All images the model returned, as one batch.",
        "The first video the model returned.",
        "The first audio file the model returned.",
        "Text from LLM and speech-to-text models.",
        "Every output URL, one per line.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {
                    "default": "openai/gpt-image-2",
                    "tooltip": "Model slug as owner/project, as in wiro.ai/models/owner/project.",
                }),
                "prompt": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "Sent as the prompt parameter when not empty.",
                }),
                "parameters": ("STRING", {
                    "multiline": True, "default": DEFAULT_PARAMETERS,
                    "tooltip": "Other model parameters as a JSON object. The model page on wiro.ai lists them.",
                }),
            },
            "optional": {
                "wiro_auth": AUTH_INPUT,
                "image": ("IMAGE", {"tooltip": "Uploaded to the model's first image input. A batch sends every image."}),
                "image_2": ("IMAGE", {"tooltip": "Uploaded to the model's second image input, such as a last frame."}),
                "audio": ("AUDIO", {"tooltip": "Uploaded to the model's audio input as WAV."}),
                "video": ("VIDEO", {"tooltip": "Uploaded to the model's video input as MP4."}),
                "image_field": ("STRING", {"default": "auto", "advanced": True,
                                           "tooltip": "Parameter that receives image, or auto."}),
                "image_2_field": ("STRING", {"default": "auto", "advanced": True,
                                             "tooltip": "Parameter that receives image_2, or auto."}),
                "audio_field": ("STRING", {"default": "auto", "advanced": True,
                                           "tooltip": "Parameter that receives audio, or auto."}),
                "video_field": ("STRING", {"default": "auto", "advanced": True,
                                           "tooltip": "Parameter that receives video, or auto."}),
                "timeout": TIMEOUT_INPUT,
                "force_rerun": FORCE_RERUN_INPUT,
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, force_rerun=False, **kwargs):
        return time.time() if force_rerun else ""

    def run(
        self, model, prompt, parameters, wiro_auth=None, image=None, image_2=None, audio=None,
        video=None, image_field="auto", image_2_field="auto", audio_field="auto",
        video_field="auto", timeout=1800, force_rerun=False, unique_id=None,
    ):
        credentials = resolve_credentials(wiro_auth)
        try:
            params = parse_parameters(parameters)
        except ValueError as exc:
            raise WiroError(str(exc)) from None
        if prompt and prompt.strip():
            params["prompt"] = prompt

        owner, project = parse_model(model)
        slug = f"{owner}/{project}"
        client = WiroClient(credentials)
        info = runner.model_info(client, owner, project)
        file_params = info.file_params if info else None
        if info:
            # Required settings left out of the JSON get the model's defaults,
            # as on the model page.
            for name, value in info.required_defaults.items():
                params.setdefault(name, value)
        uploads = []
        if any(x is not None for x in (image, image_2, audio, video)):
            try:
                if image is not None:
                    field = pick_file_field(file_params, "image", explicit=image_field, model=slug)
                    uploads += runner.image_uploads(field, image, single=_single(file_params, field))
                if image_2 is not None:
                    field = pick_file_field(file_params, "image", index=1, explicit=image_2_field, model=slug)
                    uploads += runner.image_uploads(field, image_2, single=_single(file_params, field))
                if audio is not None:
                    field = pick_file_field(file_params, "audio", explicit=audio_field, model=slug)
                    uploads.append(runner.audio_upload(field, audio))
                if video is not None:
                    field = pick_file_field(file_params, "video", explicit=video_field, model=slug)
                    uploads.append(runner.video_upload(field, video))
            except ValueError as exc:
                raise WiroError(str(exc)) from None

        result = runner.run_task(
            credentials, slug, params, uploads, file_params=file_params,
            timeout=timeout, node_id=unique_id, client=client,
        )
        summary = result.summary()
        images = runner.result_images(result)
        video_out = runner.result_video(result)
        audio_out = runner.result_audio(result)
        return (
            images if images is not None else runner.blocker(f"{slug} returned no image ({summary})."),
            video_out if video_out is not None else runner.blocker(f"{slug} returned no video ({summary})."),
            audio_out if audio_out is not None else runner.blocker(f"{slug} returned no audio ({summary})."),
            runner.result_text(result),
            "\n".join(result.urls),
        )


def _single(file_params, field) -> bool:
    return bool(file_params) and file_params.get(field) == "fileinput"


# ---------------------------------------------------------------- model nodes

KIND_OUTPUTS = {
    "image": (("IMAGE", "STRING"), ("images", "urls")),
    "video": (("VIDEO", "STRING"), ("video", "urls")),
    "audio": (("AUDIO", "STRING"), ("audio", "urls")),
    "text": (("STRING", "STRING"), ("text", "urls")),
}
KIND_CATEGORY = {"image": "Image", "video": "Video", "audio": "Audio", "text": "Audio"}
FILE_TYPES = ("fileinput", "multifileinput", "combinefileinput")


def _file_socket(param_id: str) -> str:
    lowered = param_id.lower()
    if "audio" in lowered:
        return "AUDIO"
    if "video" in lowered:
        return "VIDEO"
    return "IMAGE"


def _is_int(*values) -> bool:
    return all(v is None or (isinstance(v, (int, float)) and float(v).is_integer()) for v in values)


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def display_name(param: dict):
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


def widget_for(param: dict):
    """ComfyUI input spec for one Wiro parameter."""
    ptype = param["type"]
    pid = param["id"]
    options = {"tooltip": param.get("note") or param.get("label") or pid}
    label = display_name(param)
    if label:
        options["display_name"] = label
    if param.get("advanced"):
        options["advanced"] = True
    default = param.get("default", "")

    if ptype in FILE_TYPES:
        return (_file_socket(pid), options)
    if ptype == "select":
        choices = list(param.get("options") or [])
        if default not in choices:
            if not param.get("required") and "" not in choices:
                choices.insert(0, "")
            default = "" if "" in choices else (choices[0] if choices else "")
        options["default"] = default
        return (choices, options)
    if ptype == "checkbox":
        options["default"] = str(default).lower() == "true"
        return ("BOOLEAN", options)
    if ptype in ("number", "float", "range"):
        low, high, step = param.get("min"), param.get("max"), param.get("step")
        try:
            value = float(default)
        except (TypeError, ValueError):
            value = float(low) if low is not None else 0.0
        if ptype != "float" and _is_int(low, high, step, value):
            options.update(default=int(value), step=int(step or 1))
            if low is not None:
                options["min"] = int(low)
            if high is not None:
                options["max"] = int(high)
            if pid == "seed":
                options["control_after_generate"] = "fixed"
                options["tooltip"] = (options["tooltip"] + " 0 lets the model pick a seed.").strip()
            return ("INT", options)
        options.update(default=value, step=float(step or 0.01), round=False)
        if low is not None:
            options["min"] = float(low)
        if high is not None:
            options["max"] = float(high)
        return ("FLOAT", options)
    # text and textarea
    multiline = ptype == "textarea"
    options["multiline"] = multiline
    options["default"] = "" if pid == "prompt" else str(default or "")
    return ("STRING", options)


def wiro_value(param: dict, value: Any):
    """Value to send for a widget, or None to leave the parameter out."""
    ptype = param["type"]
    if ptype == "checkbox":
        return "true" if value else "false"
    if ptype in ("number", "float", "range"):
        if param["id"] == "seed" and not value:
            return None
        return value
    if isinstance(value, str) and value == "" and not param.get("required"):
        return None
    return value


def make_model_node(spec: dict):
    kind = spec["kind"]
    return_types, return_names = KIND_OUTPUTS[kind]
    params = spec["parameters"]
    model = spec["model"]
    file_types = {p["id"]: p["type"] for p in params if p["type"] in FILE_TYPES}

    def input_types(cls):
        required, optional = {}, {}
        for param in params:
            spec_ = widget_for(param)
            if param["type"] in FILE_TYPES:
                optional[param["id"]] = spec_
            else:
                required[param["id"]] = spec_
        optional["wiro_auth"] = AUTH_INPUT
        optional["timeout"] = TIMEOUT_INPUT
        optional["force_rerun"] = FORCE_RERUN_INPUT
        return {"required": required, "optional": optional, "hidden": {"unique_id": "UNIQUE_ID"}}

    def is_changed(cls, force_rerun=False, **kwargs):
        return time.time() if force_rerun else ""

    def run(self, wiro_auth=None, timeout=1800, force_rerun=False, unique_id=None, **inputs):
        credentials = resolve_credentials(wiro_auth)
        values, uploads = {}, []
        for param in params:
            pid = param["id"]
            value = inputs.get(pid)
            if param["type"] in FILE_TYPES:
                if value is None:
                    continue
                socket = _file_socket(pid)
                if socket == "IMAGE":
                    files = runner.image_uploads(pid, value, single=param["type"] == "fileinput")
                    limit = param.get("max_files")
                    uploads += files[:limit] if limit else files
                elif socket == "AUDIO":
                    uploads.append(runner.audio_upload(pid, value))
                else:
                    uploads.append(runner.video_upload(pid, value))
                continue
            if value is None:
                continue
            converted = wiro_value(param, value)
            if converted is not None:
                values[pid] = converted

        result = runner.run_task(
            credentials, model, values, uploads, file_params=file_types,
            timeout=timeout, node_id=unique_id,
        )
        if kind == "image":
            main = runner.result_images(result)
        elif kind == "video":
            main = runner.result_video(result)
        elif kind == "audio":
            main = runner.result_audio(result)
        else:
            main = runner.result_text(result)
        if main is None or (kind == "text" and not main and not result.files):
            raise WiroError(f"{model} finished but returned no {kind} ({result.summary()}).")
        return (main, "\n".join(result.urls))

    description = (
        f"{spec['description']} Runs {model} on Wiro AI; each run is billed to your Wiro AI "
        f"account. Price and parameters: https://wiro.ai/models/{model}"
    )
    return type(spec["node"], (), {
        "DESCRIPTION": description,
        "CATEGORY": f"{CATEGORY}/{KIND_CATEGORY[kind]}",
        "FUNCTION": "run",
        "RETURN_TYPES": return_types,
        "RETURN_NAMES": return_names,
        "INPUT_TYPES": classmethod(input_types),
        "IS_CHANGED": classmethod(is_changed),
        "run": run,
        "WIRO_MODEL": model,
    })


def load_model_specs() -> list:
    path = os.path.join(os.path.dirname(__file__), "models.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["models"]


NODE_CLASS_MAPPINGS = {
    "WiroApiKey": WiroApiKey,
    "WiroRunModel": WiroRunModel,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "WiroApiKey": "Wiro API Key",
    "WiroRunModel": "Wiro Run Model",
}
for _spec in load_model_specs():
    NODE_CLASS_MAPPINGS[_spec["node"]] = make_model_node(_spec)
    NODE_DISPLAY_NAME_MAPPINGS[_spec["node"]] = _spec["display_name"]
