"""Turns node inputs into a Wiro Run request.

Text-only runs are sent as JSON. Runs with files are sent as multipart form
data, following https://wiro.ai/docs/model-parameters:

- fileinput: the file goes in {id}, and {id}Url is sent empty.
- multifileinput: the files go in {id}, and the field that puts "Url" before
  "Multiple" (inputDocumentMultiple -> inputDocumentUrlMultiple) is sent empty.
- combinefileinput: files and URLs both go in {id}, with no URL field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

FILE_TYPES = ("fileinput", "multifileinput", "combinefileinput")


@dataclass
class Upload:
    """One file to send in a multipart field."""

    field: str
    filename: str
    content: bytes
    mime: str

    def __repr__(self) -> str:  # keep file bytes out of logs and error reports
        return f"Upload({self.field!r}, {self.filename!r}, {len(self.content)} bytes)"


@dataclass
class RunRequest:
    json_body: Optional[dict] = None
    form_fields: Optional[list] = None
    files: Optional[list] = None
    uploads: list = field(default_factory=list)

    @property
    def is_multipart(self) -> bool:
        return self.files is not None


def url_field(param_id: str, param_type: Optional[str]) -> Optional[str]:
    """The companion URL field of a file parameter, if it has one."""
    if param_type == "fileinput":
        return f"{param_id}Url"
    if param_type == "multifileinput":
        if param_id.endswith("Multiple"):
            return param_id[: -len("Multiple")] + "UrlMultiple"
        return f"{param_id}Url"
    return None


def form_value(value: Any) -> str:
    """How a parameter value is written in a multipart field."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def build_run_request(
    params: Optional[dict],
    uploads: Iterable[Upload] = (),
    file_types: Optional[dict] = None,
) -> RunRequest:
    """Build the Run body.

    params: parameter values (None values are left out).
    uploads: files to send, each tagged with its parameter id.
    file_types: parameter id -> Wiro parameter type, for the model's file
        parameters (from Tool/Detail). Unknown fields are treated as
        combinefileinput, which takes files directly in the field.
    """
    params = {k: v for k, v in (params or {}).items() if v is not None}
    uploads = list(uploads)
    file_types = dict(file_types or {})

    if not uploads:
        return RunRequest(json_body=params)

    upload_fields = {u.field for u in uploads}
    url_fields_of_uploads = {
        url_field(f, file_types.get(f)) for f in upload_fields
    } - {None}

    form: list[tuple[str, str]] = []
    for key, value in params.items():
        ptype = file_types.get(key)
        if key in upload_fields and ptype in ("fileinput", "multifileinput"):
            continue  # the uploaded file takes this field
        if key in url_fields_of_uploads:
            continue  # the uploaded file wins over a URL for the same parameter
        if ptype == "combinefileinput" and isinstance(value, (list, tuple)):
            form.extend((key, str(v)) for v in value if str(v).strip())
        else:
            form.append((key, form_value(value)))

    present = {k for k, _ in form}
    for param_id, ptype in file_types.items():
        companion = url_field(param_id, ptype)
        if companion is None:
            continue
        if param_id in upload_fields:
            form.append((companion, ""))
        elif companion in present and param_id not in present:
            # A URL was given for this parameter: send the file field empty.
            form.append((param_id, ""))
    # A file field whose parameter type is unknown still gets no URL field,
    # because combinefileinput (the usual type for images) has none.

    files = [(u.field, (u.filename, u.content, u.mime)) for u in uploads]
    return RunRequest(form_fields=form, files=files, uploads=uploads)


def parse_parameters(text: str) -> dict:
    """Parse the JSON object typed into the parameters box."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"parameters is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from None
    if not isinstance(value, dict):
        raise ValueError('parameters must be a JSON object, like {"prompt": "a red fox"}')
    return value


def file_params_from_tool(tool: dict) -> dict:
    """Parameter id -> type for every file parameter in a Tool/Detail response."""
    result: dict[str, str] = {}
    for group in tool.get("parameters") or []:
        for item in (group or {}).get("items") or []:
            ptype = item.get("type")
            pid = item.get("id")
            if pid and ptype in FILE_TYPES:
                result[str(pid)] = str(ptype)
    return result


def required_defaults_from_tool(tool: dict) -> dict:
    """Defaults of the required settings (selects, numbers, checkboxes) in a
    Tool/Detail response, so a run that leaves them out still validates.
    Text, JSON and file parameters are never filled in."""
    result: dict = {}
    for group in tool.get("parameters") or []:
        for item in (group or {}).get("items") or []:
            pid, ptype = item.get("id"), item.get("type")
            default = item.get("defaultvalue")
            if not pid or not item.get("required") or default in (None, ""):
                continue
            if ptype in ("number", "float", "range"):
                try:
                    number = float(default)
                except (TypeError, ValueError):
                    continue
                result[str(pid)] = int(number) if number.is_integer() and ptype != "float" else number
            elif ptype in ("select", "checkbox"):
                result[str(pid)] = str(default)
    return result


def pick_file_field(
    file_params: Optional[dict],
    kind: str,
    *,
    index: int = 0,
    explicit: str = "auto",
    model: str = "",
) -> str:
    """Choose which file parameter a connected image, audio or video goes to.

    explicit: a parameter id typed by the user, or "auto".
    index: 0 for the first input of this kind, 1 for the second (image_2).
    """
    explicit = (explicit or "auto").strip()
    if explicit and explicit.lower() != "auto":
        if file_params and explicit not in file_params:
            raise ValueError(
                f'{model or "This model"} has no file parameter "{explicit}". '
                f"Its file parameters are: {', '.join(sorted(file_params)) or 'none'}."
            )
        return explicit

    defaults = {"image": ["inputImage", "inputImage2"], "audio": ["inputAudio"], "video": ["inputVideo"]}
    if not file_params:
        names = defaults.get(kind, [])
        if index < len(names):
            return names[index]
        return names[0] if names else f"input{kind.capitalize()}"

    candidates = [
        pid for pid in file_params
        if kind in pid.lower() and "mask" not in pid.lower()
    ]
    if index < len(candidates):
        return candidates[index]
    if candidates and file_params.get(candidates[0]) in ("combinefileinput", "multifileinput"):
        # One parameter that takes several files: send the extra input there too.
        return candidates[0]
    generic = [
        pid for pid in file_params
        if any(word in pid.lower() for word in ("all", "file", "media"))
    ]
    if generic:
        return generic[0]
    raise ValueError(
        f"{model or 'This model'} has no {kind} input. "
        f"Its file parameters are: {', '.join(sorted(file_params)) or 'none'}."
    )
