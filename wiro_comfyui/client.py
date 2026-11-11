"""HTTP client for the Wiro AI API.

API reference: https://wiro.ai/docs/run-a-model and https://wiro.ai/docs/tasks
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any, Callable, Optional

import requests

from .payload import RunRequest
from .version import __version__

API_BASE = "https://api.wiro.ai/v1"
USER_AGENT = f"comfyui-wiro/{__version__}"

# Task/Detail statuses that close a task. Every other status, including a
# persisted "task_error", means the task is still in progress.
DONE_STATUSES = ("task_postprocess_end", "task_cancel")

MISSING_KEY_MESSAGE = (
    "No Wiro API key found. Set the WIRO_API_KEY environment variable before "
    "starting ComfyUI, or connect a Wiro API Key node. You can create a key at "
    "https://wiro.ai/panel/project/new"
)

log = logging.getLogger("comfyui-wiro")

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL_URL = re.compile(r"wiro\.ai/(?:models/|v1/Run/)?([^/?#\s]+)/([^/?#\s]+)")


class WiroError(RuntimeError):
    """A Wiro API call or task failed."""


class WiroTimeout(WiroError):
    """The task did not finish in time. It may still be running on Wiro."""


class WiroInterrupted(WiroError):
    """ComfyUI asked the node to stop while it was waiting for a task."""


class WiroCredentials:
    """API key, plus the API secret for projects that use signature auth.

    repr() and str() never show the values: ComfyUI prints node inputs in its
    error reports, and this object travels between nodes as an input.
    """

    __slots__ = ("_key", "_secret")

    def __init__(self, api_key: str, api_secret: str = ""):
        self._key = (api_key or "").strip()
        self._secret = (api_secret or "").strip()
        if not self._key:
            raise WiroError(MISSING_KEY_MESSAGE)

    @property
    def uses_signature(self) -> bool:
        return bool(self._secret)

    def headers(self) -> dict[str, str]:
        """Auth headers for one request (a fresh nonce for signature projects)."""
        headers = {"x-api-key": self._key}
        if self._secret:
            nonce = str(time.time_ns() // 1_000_000)
            signature = hmac.new(
                self._key.encode("utf-8"),
                (self._secret + nonce).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["x-nonce"] = nonce
            headers["x-signature"] = signature
        return headers

    def __repr__(self) -> str:
        kind = "API key and secret" if self._secret else "API key"
        return f"<Wiro credentials: {kind}>"

    __str__ = __repr__


def credentials_from_env() -> Optional[WiroCredentials]:
    key = os.environ.get("WIRO_API_KEY", "").strip()
    if not key:
        return None
    return WiroCredentials(key, os.environ.get("WIRO_API_SECRET", ""))


def resolve_credentials(auth: Any = None) -> WiroCredentials:
    """Credentials from a Wiro API Key node if connected, else the environment."""
    if isinstance(auth, WiroCredentials):
        return auth
    env = credentials_from_env()
    if env is not None:
        return env
    raise WiroError(MISSING_KEY_MESSAGE)


def parse_model(model: str) -> tuple[str, str]:
    """Split "owner/project" (or a wiro.ai model URL) into its two slugs."""
    text = (model or "").strip()
    match = _MODEL_URL.search(text)
    if match:
        owner, project = match.group(1), match.group(2)
    else:
        parts = [p for p in text.strip("/").split("/") if p]
        if len(parts) != 2:
            raise WiroError(
                'The model must look like "owner/project", for example '
                f'"openai/gpt-image-2". Got: "{model}"'
            )
        owner, project = parts
    for part in (owner, project):
        if not _SLUG.match(part):
            raise WiroError(f'"{part}" is not a valid model slug (from "{model}").')
    return owner, project


def _error_text(body: Any, status_code: int, path: str) -> str:
    errors = body.get("errors") if isinstance(body, dict) else None
    messages: list[str] = []
    codes: set[int] = set()
    for item in errors or []:
        if isinstance(item, dict):
            message = str(item.get("message") or "").strip()
            code = item.get("code")
            if isinstance(code, int):
                codes.add(code)
            elif isinstance(code, str) and code.isdigit():
                codes.add(int(code))
        else:
            message = str(item).strip()
        if message:
            messages.append(message)
    text = "; ".join(messages).rstrip(".") or f"HTTP {status_code}"
    hint = ""
    if 97 in codes:
        hint = " Add credits at https://wiro.ai/panel/billing and run again."
    elif 96 in codes:
        hint = " Wait for one of your running tasks to finish, then run again."
    elif status_code == 401:
        hint = " Check the API key (and the secret, if the project uses signature auth)."
    return f"Wiro API error on {path}: {text}.{hint}"


class WiroClient:
    """Thin wrapper over the endpoints the nodes use."""

    def __init__(
        self,
        credentials: WiroCredentials,
        *,
        base_url: str = API_BASE,
        session: Optional[requests.Session] = None,
        timeout: float = 60.0,
    ):
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def post(
        self,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: Optional[float] = None,
    ) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        headers.update(self.credentials.headers())
        try:
            response = self.session.post(
                url,
                json=json_body if files is None and data is None else None,
                data=data,
                files=files,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise WiroError(f"Could not reach the Wiro API ({path}): {exc}") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise WiroError(
                f"Wiro API returned HTTP {response.status_code} without a JSON body ({path})."
            )
        if body.get("result") is not True:
            raise WiroError(_error_text(body, response.status_code, path))
        return body

    def run(self, owner: str, project: str, request: RunRequest) -> dict:
        """Start a task. Returns the Run response (taskid, socketaccesstoken)."""
        body = self.post(
            f"Run/{owner}/{project}",
            json_body=request.json_body,
            data=request.form_fields,
            files=request.files,
            timeout=600,
        )
        if not body.get("socketaccesstoken"):
            raise WiroError("Wiro accepted the run but returned no task token.")
        return body

    def task_detail(self, token: str) -> dict:
        body = self.post("Task/Detail", json_body={"tasktoken": token})
        tasks = body.get("tasklist") or []
        if not tasks or not isinstance(tasks[0], dict):
            raise WiroError("Task/Detail returned no task.")
        return tasks[0]

    def stop(self, task: dict) -> None:
        """Cancel a queued task or kill a running one. Best effort."""
        status = task.get("status")
        task_id = str(task.get("id") or "")
        token = str(task.get("socketaccesstoken") or "")
        attempts = []
        if task_id and status in (
            None, "task_queue", "task_accept", "task_preprocess_start", "task_preprocess_end",
        ):
            attempts.append(("Task/Cancel", {"taskid": task_id}))
        if token:
            attempts.append(("Task/Kill", {"socketaccesstoken": token}))
        last_error: Optional[WiroError] = None
        for path, body in attempts:
            try:
                self.post(path, json_body=body)
                return
            except WiroError as exc:
                last_error = exc
        if last_error is not None:
            log.warning("[Wiro] could not stop task %s: %s", task_id, last_error)

    def tool_detail(self, owner: str, project: str) -> dict:
        body = self.post("Tool/Detail", json_body={"slugowner": owner, "slugproject": project})
        tools = body.get("tool") or []
        if not tools or not isinstance(tools[0], dict):
            raise WiroError(f'Wiro has no model "{owner}/{project}".')
        return tools[0]


def wait_for_task(
    client: WiroClient,
    run_response: dict,
    *,
    timeout: float,
    poll_interval: float = 2.0,
    on_update: Optional[Callable[[dict, float], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_poll_errors: int = 5,
) -> dict:
    """Poll Task/Detail until the task reaches task_postprocess_end or task_cancel.

    run_response: the body returned by Run (taskid and socketaccesstoken).
    """
    token = str(run_response.get("socketaccesstoken") or "")
    start = clock()
    task: dict = {
        "id": run_response.get("taskid"),
        "socketaccesstoken": token,
        "status": "task_queue",
    }
    errors = 0
    while True:
        if should_stop is not None and should_stop():
            client.stop(task)
            raise WiroInterrupted("Stopped by ComfyUI.")
        try:
            task = client.task_detail(token)
            errors = 0
        except WiroError:
            errors += 1
            if errors >= max_poll_errors:
                raise
        else:
            elapsed = clock() - start
            if on_update is not None:
                on_update(task, elapsed)
            if task.get("status") in DONE_STATUSES:
                return task
        elapsed = clock() - start
        if elapsed >= timeout:
            raise WiroTimeout(
                f"Wiro task {task.get('id', '')} did not finish within {int(timeout)} seconds. "
                "It keeps running on Wiro; raise the timeout to wait longer."
            )
        # Poll every few seconds, a little slower for long video jobs.
        sleep(poll_interval if elapsed < 120 else poll_interval * 2)


def ensure_success(task: dict) -> None:
    """Raise unless the task finished with pexit "0"."""
    task_id = task.get("id", "")
    if task.get("status") == "task_cancel":
        raise WiroError(f"Wiro task {task_id} was cancelled.")
    pexit = str(task.get("pexit", "")).strip()
    if pexit != "0":
        detail = str(task.get("debugerror") or "").strip()
        if len(detail) > 600:
            detail = "..." + detail[-600:]
        message = f"Wiro task {task_id} failed (exit code {pexit or 'unknown'})."
        if detail:
            message += f" {detail}"
        raise WiroError(message)
