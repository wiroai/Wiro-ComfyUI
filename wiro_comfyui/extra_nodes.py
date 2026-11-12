"""Wiro LLM (the LLM gateway at llm.wiro.ai) and Wiro Run Workflow."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Optional

import requests

from . import catalog, media, runner
from .client import (
    USER_AGENT,
    WiroClient,
    WiroCredentials,
    WiroError,
    WiroInterrupted,
    log,
    resolve_credentials,
)
from .payload import parse_parameters
from .results import OutputFile, TaskResult

LLM_BASE = "https://llm.wiro.ai/v1"
DEFAULT_LLM = "openai/gpt-5-nano"
AUTH_INPUT = (
    "WIRO_AUTH",
    {"tooltip": "Optional. Connect a Wiro API Key node. Without it the WIRO_API_KEY environment variable is used."},
)
FORCE_RERUN_INPUT = (
    "BOOLEAN",
    {"default": False, "advanced": True,
     "tooltip": "Run again on every queue, even when no input changed. Every run is billed."},
)


def _money(cost: Optional[float]) -> str:
    if cost is None:
        return ""
    return f", ${cost:.4f}" if cost >= 0.001 else f", ${cost:.6f}"


def _ui(task_id: str = "", cost: Optional[float] = None) -> dict:
    ui = {}
    if task_id:
        ui["wiro_task"] = [task_id]
    if cost is not None:
        ui["wiro_cost"] = [f"{cost:.6f}"]
    return ui


# ---------------------------------------------------------------- LLM


def llm_choices() -> list:
    ids = sorted(m["id"] for m in catalog.llm_models())
    if not ids:
        return [DEFAULT_LLM]
    if DEFAULT_LLM in ids:
        ids.remove(DEFAULT_LLM)
        ids.insert(0, DEFAULT_LLM)
    return ids


def build_llm_messages(prompt: str, system: str, images: list) -> list:
    messages = []
    if system and system.strip():
        messages.append({"role": "system", "content": system})
    if images:
        parts = [{"type": "text", "text": prompt}]
        for png in images:
            data = base64.b64encode(png).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})
        messages.append({"role": "user", "content": parts})
    else:
        messages.append({"role": "user", "content": prompt})
    return messages


def llm_answer(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise WiroError("The LLM gateway returned no answer.")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "").strip()


def call_llm(credentials: WiroCredentials, payload: dict, session=None, timeout: float = 600) -> dict:
    session = session or requests.Session()
    try:
        response = session.post(
            f"{LLM_BASE}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {credentials.bearer()}", "User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise WiroError(f"Could not reach the Wiro LLM gateway: {exc}") from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not isinstance(body, dict) or "error" in (body or {}):
        error = (body or {}).get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else (error or f"HTTP {response.status_code}")
        hint = " Check the API key." if response.status_code == 401 else ""
        raise WiroError(f"Wiro LLM gateway error: {message}.{hint}")
    return body


class WiroLLM:
    """Chat with an LLM through the Wiro LLM gateway."""

    DESCRIPTION = (
        "Asks an LLM through the Wiro AI LLM gateway (llm.wiro.ai): a prompt, an optional "
        "system instruction and optional images for models that read images. Returns the "
        "answer as text. Each call is billed to your Wiro AI account at the model's token price."
    )
    CATEGORY = "Wiro AI"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    OUTPUT_TOOLTIPS = ("The model's answer.",)

    @classmethod
    def INPUT_TYPES(cls):
        choices = llm_choices()
        return {
            "required": {
                "model": (choices, {"default": choices[0], "tooltip": "Model from the Wiro LLM gateway (llm.wiro.ai/v1/models)."}),
                "prompt": ("STRING", {"multiline": True, "default": "", "tooltip": "Your message to the model."}),
                "system_instructions": ("STRING", {"multiline": True, "default": "",
                                                   "tooltip": "Optional system message that sets how the model answers."}),
            },
            "optional": {
                "image": ("IMAGE", {"tooltip": "Optional images, for models that read images."}),
                "wiro_auth": AUTH_INPUT,
                "max_tokens": ("INT", {"default": 0, "min": 0, "max": 1_000_000, "step": 64, "advanced": True,
                                       "tooltip": "Longest answer in tokens. 0 uses the model's default."}),
                "temperature": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 2.0, "step": 0.05, "round": False,
                                          "advanced": True, "tooltip": "-1 uses the model's default."}),
                "force_rerun": FORCE_RERUN_INPUT,
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, force_rerun=False, **kwargs):
        return time.time() if force_rerun else ""

    def run(self, model, prompt, system_instructions="", image=None, wiro_auth=None, max_tokens=0,
            temperature=-1.0, force_rerun=False, unique_id=None):
        credentials = resolve_credentials(wiro_auth)
        if not (prompt or "").strip():
            raise WiroError("Write a prompt for the model.")
        images = []
        if image is not None:
            info = next((m for m in catalog.llm_models() if m["id"] == model), None)
            if info is not None and not info.get("image"):
                readers = ", ".join(m["id"] for m in catalog.llm_models() if m.get("image"))[:400]
                raise WiroError(f"{model} does not read images. Models that do: {readers}")
            images = media.image_batch_to_png(image)[:8]
        payload = {"model": model, "messages": build_llm_messages(prompt, system_instructions, images), "stream": False}
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None and temperature >= 0:
            payload["temperature"] = float(temperature)
        runner.send_progress(unique_id, f"{model}: asking")
        start = time.monotonic()
        body = call_llm(credentials, payload)
        text = llm_answer(body)
        usage = body.get("usage") or {}
        cost = usage.get("cost")
        try:
            cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        log.info("[Wiro] %s: answered in %d s, %s tokens%s", model, time.monotonic() - start,
                 usage.get("total_tokens", "?"), _money(cost))
        runner.send_progress(unique_id, f"{model}: done{_money(cost)}")
        return {"ui": _ui(str(body.get("id") or ""), cost), "result": (text,)}


# ---------------------------------------------------------------- workflows

WORKFLOW_DONE = ("completed", "failed", "stopped", "cancelled")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def parse_workflow_slug(text: str) -> str:
    text = (text or "").strip()
    match = re.search(r"wiro\.ai/workflows/(?:community/)?([^/?#\s]+)", text)
    slug = match.group(1) if match else text.strip("/")
    if not _SLUG.match(slug or ""):
        raise WiroError(f'Paste a workflow slug, such as "product-ad-a1B2c", or its wiro.ai link. Got: "{text}"')
    return slug


def workflow_fields(detail: dict) -> dict:
    contents = detail.get("contents")
    if isinstance(contents, str):
        try:
            contents = json.loads(contents)
        except ValueError:
            contents = {}
    return ((contents or {}).get("schema") or {}).get("input") or {}


def pick_workflow_field(fields: dict, kind: str, explicit: str = "auto") -> str:
    explicit = (explicit or "auto").strip()
    if explicit and explicit.lower() != "auto":
        if fields and explicit not in fields:
            raise WiroError(f'The workflow has no input "{explicit}". Its inputs are: {", ".join(fields)}.')
        return explicit
    for key, field in fields.items():
        if ((field or {}).get("ui") or {}).get("field") == kind:
            return key
    raise WiroError(f"The workflow has no {kind} input. Its inputs are: {', '.join(fields) or 'none'}.")


def _references(contents: dict) -> dict:
    output = (contents or {}).get("output")
    if not output:
        output = (((contents or {}).get("nodes") or {}).get("output") or {}).get("fields") or {}
    return output if isinstance(output, dict) else {}


def collect_workflow_outputs(run: dict, nodes: list) -> TaskResult:
    """Files and text the End node of a finished run shows."""
    contents = run.get("contents")
    if isinstance(contents, str):
        try:
            contents = json.loads(contents)
        except ValueError:
            contents = {}
    by_id = {n.get("nodeId"): n for n in nodes or [] if isinstance(n, dict)}
    result = TaskResult(task={"id": run.get("workflowtoken", "")})
    texts = []
    refs = _references(contents)
    targets = []
    for ref in refs.values():
        refs_list = ref if isinstance(ref, list) else [ref]
        for item in refs_list:
            match = re.match(r"^\$(node-[^.]+)\.(\w+)$", str(item))
            if match:
                targets.append((match.group(1), match.group(2)))
    if not targets:  # no End node fields: use every completed step's outputs
        targets = [(n.get("nodeId"), "outputs") for n in nodes or []]
    for node_id, port in targets:
        node = by_id.get(node_id) or {}
        outputs = node.get("outputs")
        if isinstance(outputs, dict) and port != "outputs":
            outputs = outputs.get(port)
        for item in outputs if isinstance(outputs, list) else [outputs]:
            if isinstance(item, dict) and item.get("url"):
                name = str(item.get("name") or item["url"])
                kind = media.media_kind(str(item.get("contenttype") or ""), name)
                result.files.append(OutputFile(url=item["url"], name=name,
                                               content_type=str(item.get("contenttype") or ""), kind=kind))
            elif isinstance(item, dict) and str(item.get("contenttype")).lower() == "raw":
                content = item.get("content") or {}
                answer = content.get("answer") if isinstance(content, dict) else content
                if isinstance(answer, list):
                    answer = "".join(str(a) for a in answer)
                if answer:
                    texts.append(str(answer).strip())
            elif isinstance(item, str) and item.strip():
                if item.startswith(("http://", "https://")):
                    result.files.append(OutputFile(url=item, name=item, content_type="",
                                                   kind=media.media_kind("", item)))
                else:
                    texts.append(item.strip())
    if texts:
        result.has_llm_output = True
        result.task["debugoutput"] = "\n\n".join(texts)
    return result


def _cost(run: dict) -> Optional[float]:
    for key in ("chargedcost", "totalcost"):
        try:
            if run.get(key) is not None:
                return float(run[key])
        except (TypeError, ValueError):
            continue
    return None


class WiroRunWorkflow:
    """Runs one of your Wiro AI workflows."""

    DESCRIPTION = (
        "Runs a Wiro AI workflow by its slug or wiro.ai link: your own workflows and workflows "
        "listed in the public gallery. Fill its inputs as JSON; connected images, audio and video "
        "are uploaded and passed to the workflow's matching inputs. Returns the workflow's results "
        "as IMAGE, VIDEO, AUDIO and text. Each step is billed to your Wiro AI account at its model's price."
    )
    CATEGORY = "Wiro AI"
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "VIDEO", "AUDIO", "STRING", "STRING")
    RETURN_NAMES = ("images", "video", "audio", "text", "urls")
    OUTPUT_TOOLTIPS = (
        "All images the workflow returned, as one batch.",
        "The first video the workflow returned.",
        "The first audio file the workflow returned.",
        "Text results, such as an LLM step's answer.",
        "Every result URL, one per line.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "workflow": ("STRING", {"default": "", "tooltip": "Workflow slug (name-a1B2c) or its wiro.ai/workflows link."}),
                "inputs": ("STRING", {"multiline": True, "default": "{}",
                                      "tooltip": "Values for the workflow's Start fields as a JSON object, keyed by field name."}),
            },
            "optional": {
                "wiro_auth": AUTH_INPUT,
                "image": ("IMAGE", {"tooltip": "Uploaded and passed to the workflow's image input."}),
                "audio": ("AUDIO", {"tooltip": "Uploaded and passed to the workflow's audio input."}),
                "video": ("VIDEO", {"tooltip": "Uploaded and passed to the workflow's video input."}),
                "image_field": ("STRING", {"default": "auto", "advanced": True, "tooltip": "Start field for image, or auto."}),
                "audio_field": ("STRING", {"default": "auto", "advanced": True, "tooltip": "Start field for audio, or auto."}),
                "video_field": ("STRING", {"default": "auto", "advanced": True, "tooltip": "Start field for video, or auto."}),
                "timeout": ("INT", {"default": 3600, "min": 60, "max": 14400, "step": 60, "advanced": True,
                                    "tooltip": "Seconds to wait for the run. A run that takes longer keeps going on Wiro."}),
                "force_rerun": FORCE_RERUN_INPUT,
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, force_rerun=False, **kwargs):
        return time.time() if force_rerun else ""

    def run(self, workflow, inputs="{}", wiro_auth=None, image=None, audio=None, video=None,
            image_field="auto", audio_field="auto", video_field="auto", timeout=3600,
            force_rerun=False, unique_id=None):
        credentials = resolve_credentials(wiro_auth)
        slug = parse_workflow_slug(workflow)
        try:
            values = parse_parameters(inputs)
        except ValueError as exc:
            raise WiroError(str(exc).replace("parameters", "inputs")) from None
        client = WiroClient(credentials)
        detail = (client.post("Workflow/Detail", json_body={"slug": slug}).get("workflow") or [{}])
        detail = detail[0] if isinstance(detail, list) else detail
        fields = workflow_fields(detail)
        for media_value, kind, explicit, make in (
            (image, "image", image_field, lambda v: ("image.png", media.image_batch_to_png(v)[0], "image/png")),
            (audio, "audio", audio_field, lambda v: ("audio.wav", media.audio_to_wav(v), "audio/wav")),
            (video, "video", video_field, lambda v: ("video.mp4", media.video_to_mp4(v), "video/mp4")),
        ):
            if media_value is None:
                continue
            field = pick_workflow_field(fields, kind, explicit)
            name, blob, mime = make(media_value)
            runner.send_progress(unique_id, f"{slug}: uploading {kind}")
            values[field] = client.upload_file(name, blob, mime)

        run = client.post("Workflow/Run", json_body={"slug": slug, "input": values})
        token = run.get("workflowtoken")
        if not token:
            raise WiroError("Wiro accepted the workflow run but returned no run token.")
        log.info("[Wiro] workflow %s: run started", slug)
        start = time.monotonic()
        while True:
            if runner._interrupted():
                runner.send_progress(unique_id, f"{slug}: stopped waiting; the run continues on Wiro")
                runner._raise_interrupt()
            body = client.post("Workflow/RunDetail", json_body={"workflowtoken": token})
            state = body.get("workflowrun") or {}
            nodes = body.get("workflowrunnodes") or []
            status = state.get("status")
            done = sum(1 for n in nodes if n.get("status") == "completed")
            elapsed = time.monotonic() - start
            runner.send_progress(unique_id, f"{slug}: {status}, {done}/{len(nodes)} steps, {int(elapsed)} s")
            if status in WORKFLOW_DONE:
                break
            if elapsed > timeout:
                raise WiroError(f"The workflow run did not finish within {int(timeout)} seconds. It keeps running on Wiro.")
            time.sleep(3 if elapsed < 120 else 6)
        if status != "completed":
            failed = next((n for n in nodes if n.get("status") == "failed"), None)
            reason = (failed or {}).get("error") or (failed or {}).get("cause") or status
            step = f" at step {failed.get('nodeId')} ({failed.get('tool')})" if failed else ""
            raise WiroError(f"The workflow run ended as {status}{step}: {reason}")
        result = collect_workflow_outputs(state, nodes)
        cost = _cost(state)
        log.info("[Wiro] workflow %s: run done in %d s, %d steps%s", slug, time.monotonic() - start,
                 len(nodes), _money(cost))
        runner.send_progress(unique_id, f"{slug}: done{_money(cost)}")
        summary = result.summary()
        images = runner.result_images(result)
        video_out = runner.result_video(result)
        audio_out = runner.result_audio(result)
        outputs = (
            images if images is not None else runner.blocker(f"The workflow returned no image ({summary})."),
            video_out if video_out is not None else runner.blocker(f"The workflow returned no video ({summary})."),
            audio_out if audio_out is not None else runner.blocker(f"The workflow returned no audio ({summary})."),
            result.llm_text() if result.has_llm_output else "",
            "\n".join(result.urls),
        )
        return {"ui": _ui(token, cost), "result": outputs}
