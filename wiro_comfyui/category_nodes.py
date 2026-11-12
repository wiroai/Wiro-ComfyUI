"""Category nodes (Text to Image, Image to Video, Text to Speech, ...).

Each node has a model menu filled from the Wiro AI catalog. Picking a model
shows that model's own settings with its choices and defaults, using
ComfyUI's dynamic combo input. The inputs every model of the category shares
stay at the top of the node.

These nodes use ComfyUI's V3 node API (comfy_api.latest). On a ComfyUI
version without it, build_nodes() returns nothing and the other Wiro nodes
keep working.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional

from . import catalog, media, roles, runner
from .client import WiroError, resolve_credentials
from .widgets import FILE_TYPES, display_name, number_spec, select_choices, wiro_value

log = logging.getLogger("comfyui-wiro")

AUTH_TOOLTIP = "Optional. Connect a Wiro API Key node. Without it the WIRO_API_KEY environment variable is used."
RESERVED_IDS = {"model"}
ROLE_INPUTS = {
    # role: (input id, socket, tooltip)
    "images": ("images", "IMAGE", "Reference images. A batch sends every image, up to the model's limit."),
    "image": ("image", "IMAGE", "The input image."),
    "mask": ("mask", "IMAGE", "Optional mask image for the edit, for models that take one."),
    "first_frame": ("first_frame", "IMAGE", "The first frame of the video."),
    "last_frame": ("last_frame", "IMAGE", "Optional last frame, for models that take one."),
    "video": ("video", "VIDEO", "The input video."),
    "audio": ("audio", "AUDIO", "The input audio, uploaded as WAV."),
}


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _alias(pid: str) -> str:
    """Widget name for a model setting: snake_case like ComfyUI's own inputs
    (the frontend labels dynamic inputs by name), mapped back on send."""
    name = re.sub(r"[^a-z0-9_]+", "_", _CAMEL.sub("_", pid).lower()).strip("_") or "setting"
    return f"{name}_setting" if name in RESERVED_IDS else name


def setting_aliases(params: list) -> dict:
    """Parameter id -> widget name for one model, unique within the model."""
    aliases, used = {}, set()
    for param in params:
        name = base = _alias(param["id"])
        count = 2
        while name in used:
            name = f"{base}_{count}"
            count += 1
        used.add(name)
        aliases[param["id"]] = name
    return aliases


def _io():
    try:
        from comfy_api.latest import io
    except ImportError:
        return None
    if not hasattr(io, "DynamicCombo"):
        return None
    return io


def setting_input(io, param: dict, name: Optional[str] = None):
    """V3 input for one model setting, or None to leave it to the model."""
    ptype = param["type"]
    common = {
        "display_name": display_name(param),
        "tooltip": param.get("note") or param.get("label") or param["id"],
        "advanced": True if param.get("advanced") else None,
    }
    pid = name or _alias(param["id"])
    if ptype == "select":
        choices, default = select_choices(param)
        return io.Combo.Input(pid, options=choices, default=default, **common)
    if ptype == "checkbox":
        return io.Boolean.Input(pid, default=str(param.get("default", "")).lower() == "true", **common)
    if ptype in ("number", "float", "range"):
        spec = number_spec(param)
        if spec is None:
            return None
        if spec["kind"] == "INT":
            if param["id"] == "seed":
                common["tooltip"] = (common["tooltip"] + " 0 lets the model pick a seed.").strip()
            # No "control after generate" widget inside a model menu: the frontend
            # leaves a stray copy behind each time the model or the workflow changes.
            return io.Int.Input(pid, default=spec["default"], min=spec.get("min"), max=spec.get("max"),
                                step=spec["step"], control_after_generate=False, **common)
        return io.Float.Input(pid, default=spec["default"], min=spec.get("min"), max=spec.get("max"),
                              step=spec["step"], round=False, **common)
    if ptype in ("json", "json-array"):
        common["advanced"] = True
        return io.String.Input(pid, multiline=True, default="", **common)
    # text and textarea: Wiro's defaults are samples, so start empty
    return io.String.Input(pid, multiline=ptype == "textarea", default="", **common)


def file_input(io, param: dict, name: Optional[str] = None):
    socket = {"image": io.Image, "audio": io.Audio, "video": io.Video}[roles.file_kind(param)]
    return socket.Input(name or _alias(param["id"]), optional=True, display_name=display_name(param),
                        tooltip=param.get("note") or param.get("label") or param["id"])


class _OptionCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries: dict = {}

    def get(self, io, spec: dict):
        key = spec["node"]
        version = catalog.version()
        with self.lock:
            cached = self.entries.get(key)
            if cached and cached[0] == version:
                return cached[1]
        picked = roles.models_for(spec, catalog.models())
        options, used_roles = [], set()
        for model, plan in picked:
            inputs = []
            aliases = setting_aliases(plan["settings"] + plan["extra_files"])
            for param in plan["settings"]:
                built = setting_input(io, param, aliases[param["id"]])
                if built is not None:
                    inputs.append(built)
            for param in plan["extra_files"]:
                inputs.append(file_input(io, param, aliases[param["id"]]))
            options.append(io.DynamicCombo.Option(model["slug"], inputs))
            used_roles.update(plan["roles"])
        result = (options, used_roles)
        with self.lock:
            self.entries[key] = (version, result)
        return result


_options = _OptionCache()


def _uploads(param: dict, kind: str, value: Any) -> list:
    if kind == "IMAGE":
        files = runner.image_uploads(param["id"], value, single=param["type"] == "fileinput")
        limit = param.get("max_files")
        return files[:limit] if limit else files
    if kind == "AUDIO":
        return [runner.audio_upload(param["id"], value)]
    return [runner.video_upload(param["id"], value)]


def build_request(spec: dict, model_value: dict, top: dict) -> tuple:
    """(slug, values, uploads, file types, plan) for one run of a category node."""
    slug = (model_value or {}).get("model")
    model = catalog.model(slug) if slug else None
    if model is None:
        raise WiroError(f'"{slug}" is not in the Wiro AI catalog right now. Pick another model.')
    plan = roles.plan_model(spec, model)
    if plan is None:
        raise WiroError(f"{slug} cannot run from {spec['display']}.")
    values, uploads = {}, []
    aliases = setting_aliases(plan["settings"] + plan["extra_files"])
    for role, param in plan["roles"].items():
        value = top.get(role)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if role == "text":
            values[param["id"]] = value
        else:
            uploads += _uploads(param, roles.ROLE_SOCKETS[role], value)
    for param in plan["settings"]:
        key = aliases[param["id"]]
        if key in model_value and model_value[key] is not None:
            converted = wiro_value(param, model_value[key])
            if converted is not None:
                values[param["id"]] = converted
    for param in plan["extra_files"]:
        value = model_value.get(aliases[param["id"]])
        if value is not None:
            uploads += _uploads(param, roles.file_kind(param).upper(), value)
    missing = [r for r, required in spec["roles"].items() if required and r != "text"
               and r in plan["roles"] and top.get(r) is None]
    if missing:
        raise WiroError(f"Connect the {', '.join(ROLE_INPUTS[r][0] for r in missing)} input.")
    file_types = {p["id"]: p["type"] for p in model["params"] if p["type"] in FILE_TYPES}
    return slug, values, uploads, file_types, plan


def outputs_for(kind: str, result) -> tuple:
    if kind == "image":
        images = runner.result_images(result)
        if images is None:
            raise WiroError(f"The model returned no image ({result.summary()}).")
        return (images,)
    if kind == "image_mask":
        files = result.of_kind("image")
        if not files:
            raise WiroError(f"The model returned no image ({result.summary()}).")
        images, mask = media.decode_images_with_alpha([media.download(f.url) for f in files])
        return (images, mask)
    if kind == "video":
        video = runner.result_video(result)
        if video is None:
            raise WiroError(f"The model returned no video ({result.summary()}).")
        return (video,)
    if kind == "audio":
        audio = runner.result_audio(result)
        if audio is None:
            raise WiroError(f"The model returned no audio ({result.summary()}).")
        return (audio,)
    if kind == "text":
        return (runner.result_text(result),)
    if kind == "3d":
        model_file = runner.result_3d(result)
        if model_file is None:
            raise WiroError(f"The model returned no 3D file ({result.summary()}).")
        return (model_file,)
    raise WiroError(f"Unknown output kind {kind}")


def cost_ui(result) -> dict:
    ui = {"wiro_task": [result.task_id]}
    if result.cost is not None:
        ui["wiro_cost"] = [f"{result.cost:.6f}"]
    return ui


def make_category_node(io, spec: dict):
    kind = spec["kind"]
    text_id = spec.get("text_input", "prompt")
    WiroAuth = io.Custom("WIRO_AUTH")

    def outputs():
        if kind == "image":
            main = [io.Image.Output("images", tooltip="Every image the model returned, as one batch.")]
        elif kind == "image_mask":
            main = [io.Image.Output("images", tooltip="The cut-out images."),
                    io.Mask.Output("mask", tooltip="Alpha mask: 1 where the subject is.")]
        elif kind == "video":
            main = [io.Video.Output("video")]
        elif kind == "audio":
            main = [io.Audio.Output("audio")]
        elif kind == "text":
            main = [io.String.Output("text")]
        else:
            main = [io.File3DGLB.Output("model_3d", display_name="GLB")]
        return main + [io.String.Output("urls", tooltip="Every output URL, one per line.")]

    class Node(io.ComfyNode):
        @classmethod
        def define_schema(cls):
            options, used_roles = _options.get(io, spec)
            inputs = []
            if "text" in used_roles:
                inputs.append(io.String.Input(text_id, multiline=True, default="",
                                              tooltip="Sent as the model's prompt or text."))
            for role in spec["roles"]:
                if role == "text" or role not in used_roles:
                    continue
                rid, socket, tooltip = ROLE_INPUTS[role]
                socket_type = {"IMAGE": io.Image, "VIDEO": io.Video, "AUDIO": io.Audio}[socket]
                inputs.append(socket_type.Input(rid, optional=True, tooltip=tooltip))
            if options:
                inputs.append(io.DynamicCombo.Input(
                    "model", options=options,
                    tooltip="Model from the Wiro AI catalog. Its settings appear below. "
                            "Price and details: wiro.ai/models/<model>",
                ))
            else:
                inputs.append(io.Combo.Input("model", options=["(catalog not loaded)"]))
            inputs += [
                WiroAuth.Input("wiro_auth", optional=True, tooltip=AUTH_TOOLTIP),
                io.Int.Input("timeout", default=1800, min=30, max=7200, step=30, advanced=True,
                             tooltip="Seconds to wait for the task. A task that takes longer keeps running on Wiro."),
                io.Boolean.Input("force_rerun", default=False, advanced=True,
                                 tooltip="Run again on every queue, even when no input changed. Every run is billed."),
            ]
            return io.Schema(
                node_id=spec["node"],
                display_name=spec["display"],
                category=f"Wiro AI/{spec['menu']}",
                description=spec["description"] + " Each run is billed to your Wiro AI account at the model's price.",
                search_aliases=["wiro", spec["display"].replace("Wiro ", "").lower()],
                inputs=inputs,
                outputs=outputs(),
                hidden=[io.Hidden.unique_id],
            )

        @classmethod
        def fingerprint_inputs(cls, force_rerun=False, **kwargs):
            return time.time() if force_rerun else ""

        @classmethod
        def execute(cls, model=None, wiro_auth=None, timeout=1800, force_rerun=False, **kwargs) -> Any:
            credentials = resolve_credentials(wiro_auth)
            if not isinstance(model, dict):
                raise WiroError("The model catalog is not loaded yet. Reload the page and try again.")
            top = {"text": kwargs.get(text_id)}
            for role in spec["roles"]:
                if role != "text":
                    top[role] = kwargs.get(ROLE_INPUTS[role][0])
            slug, values, uploads, file_types, _ = build_request(spec, model, top)
            node_id = getattr(cls.hidden, "unique_id", None) if cls.hidden else None
            result = runner.run_task(credentials, slug, values, uploads, file_params=file_types,
                                     timeout=timeout, node_id=node_id)
            main = outputs_for(kind, result)
            return io.NodeOutput(*main, "\n".join(result.urls), ui=cost_ui(result))

    Node.__name__ = spec["node"]
    Node.__qualname__ = spec["node"]
    return Node


def build_nodes() -> tuple:
    """({node id: class}, {node id: display name}) for this ComfyUI, or empty
    dicts when its node API has no dynamic combo input."""
    io = _io()
    if io is None:
        log.warning("[Wiro] this ComfyUI version has no dynamic combo input; "
                    "the Wiro category nodes are not loaded. Update ComfyUI to use them.")
        return {}, {}
    classes, names = {}, {}
    for spec in roles.CATEGORY_SPECS:
        classes[spec["node"]] = make_category_node(io, spec)
        names[spec["node"]] = spec["display"]
    return classes, names
