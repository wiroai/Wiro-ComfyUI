"""Runs one Wiro task for a node: submit, wait with progress, read outputs."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from . import media
from .client import (
    WiroClient,
    WiroCredentials,
    WiroError,
    WiroInterrupted,
    ensure_success,
    parse_model,
    wait_for_task,
)
from .payload import Upload, build_run_request, file_params_from_tool, required_defaults_from_tool
from .results import TaskResult, read_task

log = logging.getLogger("comfyui-wiro")

STATUS_WORDS = {
    "task_queue": "queued",
    "task_accept": "preparing",
    "task_preprocess_start": "preparing inputs",
    "task_preprocess_end": "preparing",
    "task_assign": "starting",
    "task_start": "running",
    "task_output": "running",
    "task_output_full": "running",
    "task_error": "retrying setup",
    "task_end": "finishing",
    "task_postprocess_start": "saving outputs",
    "task_postprocess_end": "done",
    "task_cancel": "cancelled",
}

class ModelInfo:
    """What the generic node needs from a model's Tool/Detail record."""

    def __init__(self, file_params: dict, required_defaults: dict):
        self.file_params = file_params
        self.required_defaults = required_defaults


_info_cache: dict = {}
_cache_lock = threading.Lock()


def model_info(client: WiroClient, owner: str, project: str) -> Optional[ModelInfo]:
    """File parameters and required defaults of a model, from Tool/Detail,
    cached per model. None if Tool/Detail could not be read."""
    key = f"{owner}/{project}".lower()
    with _cache_lock:
        if key in _info_cache:
            return _info_cache[key]
    try:
        tool = client.tool_detail(owner, project)
    except WiroError as exc:
        log.warning("[Wiro] could not read the parameters of %s: %s", key, exc)
        return None
    info = ModelInfo(file_params_from_tool(tool), required_defaults_from_tool(tool))
    with _cache_lock:
        _info_cache[key] = info
    return info


def image_uploads(field: str, images: Any, single: bool = False) -> list:
    blobs = media.image_batch_to_png(images)
    if single:
        blobs = blobs[:1]
    return [Upload(field, f"image-{i + 1}.png", blob, "image/png") for i, blob in enumerate(blobs)]


def audio_upload(field: str, audio: Any) -> Upload:
    return Upload(field, "audio.wav", media.audio_to_wav(audio), "audio/wav")


def video_upload(field: str, video: Any) -> Upload:
    return Upload(field, "video.mp4", media.video_to_mp4(video), "video/mp4")


def send_progress(node_id: Optional[str], text: str) -> None:
    if not node_id:
        return
    try:
        from server import PromptServer

        PromptServer.instance.send_progress_text(text, node_id)
    except Exception:
        pass


def _interrupted() -> bool:
    try:
        import comfy.model_management as mm

        return bool(mm.processing_interrupted())
    except Exception:
        return False


def _raise_interrupt() -> None:
    try:
        import comfy.model_management as mm

        mm.throw_exception_if_processing_interrupted()
    except ImportError:
        pass
    raise WiroInterrupted("Stopped by ComfyUI.")


def run_task(
    credentials: WiroCredentials,
    model: str,
    params: dict,
    uploads: list,
    *,
    file_params: Optional[dict] = None,
    timeout: float = 1800,
    node_id: Optional[str] = None,
    client: Optional[WiroClient] = None,
) -> TaskResult:
    owner, project = parse_model(model)
    slug = f"{owner}/{project}"
    client = client or WiroClient(credentials)
    if uploads and file_params is None:
        info = model_info(client, owner, project)
        file_params = info.file_params if info else None
    request = build_run_request(params, uploads, file_params or {})

    send_progress(node_id, f"{slug}: sending")
    run = client.run(owner, project, request)
    task_id = run.get("taskid")
    log.info("[Wiro] %s: task %s started", slug, task_id)

    def on_update(task: dict, elapsed: float) -> None:
        word = STATUS_WORDS.get(str(task.get("status")), str(task.get("status") or ""))
        send_progress(node_id, f"{slug}: {word}, {int(elapsed)} s (task {task_id})")

    try:
        task = wait_for_task(
            client, run, timeout=timeout, on_update=on_update, should_stop=_interrupted
        )
    except WiroInterrupted:
        send_progress(node_id, f"{slug}: stopped (task {task_id})")
        _raise_interrupt()
        raise
    ensure_success(task)
    result = read_task(task)
    cost = result.cost
    cost_text = f", ${cost:.4f}" if cost is not None else ""
    elapsed = task.get("elapsedseconds")
    elapsed_text = f" in {float(elapsed):.0f} s" if _is_number(elapsed) else ""
    log.info("[Wiro] %s: task %s done%s%s (%s)", slug, task_id, elapsed_text, cost_text, result.summary())
    send_progress(node_id, f"{slug}: done{elapsed_text}{cost_text}. {result.summary()}")
    return result


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- outputs


def result_images(result: TaskResult):
    files = result.of_kind("image")
    if not files:
        return None
    return media.decode_images([media.download(f.url) for f in files])


def result_video(result: TaskResult):
    files = result.of_kind("video")
    if not files:
        return None
    first = files[0]
    return media.video_from_bytes(media.download(first.url), first.name or first.url)


def result_audio(result: TaskResult):
    files = result.of_kind("audio")
    if not files:
        return None
    return media.decode_audio(media.download(files[0].url))


def result_3d(result: TaskResult):
    files = result.of_kind("3d")
    if not files:
        return None
    glb = [f for f in files if f.name.lower().endswith(".glb") or f.url.lower().split("?")[0].endswith(".glb")]
    first = (glb or files)[0]
    return media.file3d_from_bytes(media.download(first.url), first.name or first.url)


def result_text(result: TaskResult) -> str:
    """LLM answer, else the content of text files, else the task's text output
    when the model returned no files at all (some speech models print text)."""
    if result.has_llm_output:
        return result.llm_text()
    texts = [media.decode_text(media.download(f.url)) for f in result.of_kind("text")]
    texts = [t for t in texts if t]
    if texts:
        return "\n\n".join(texts)
    if not result.files:
        return str(result.task.get("debugoutput") or "").strip()
    return ""


def blocker(message: Optional[str]):
    """Value for an output the model did not produce: nodes connected to it do
    not run and show the message instead."""
    try:
        from comfy_execution.graph_utils import ExecutionBlocker
    except ImportError:
        try:
            from comfy_execution.graph import ExecutionBlocker
        except ImportError:
            return None
    return ExecutionBlocker(message)
