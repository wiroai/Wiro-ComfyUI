import hashlib
import hmac

import pytest
import requests

from helpers import FakeResponse
from wiro_comfyui.client import (
    WiroClient,
    WiroCredentials,
    WiroError,
    WiroInterrupted,
    WiroTimeout,
    ensure_success,
    parse_model,
    resolve_credentials,
    wait_for_task,
)
from wiro_comfyui.payload import Upload, build_run_request

KEY = "test-key-0123456789"
SECRET = "test-secret-abcdef"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("openai/gpt-image-2", ("openai", "gpt-image-2")),
        ("  google/nano-banana-pro/ ", ("google", "nano-banana-pro")),
        ("https://wiro.ai/models/google/veo3-1", ("google", "veo3-1")),
        ("https://wiro.ai/models/klingai/kling-v3?tab=api", ("klingai", "kling-v3")),
        ("https://api.wiro.ai/v1/Run/elevenlabs/eleven-v4", ("elevenlabs", "eleven-v4")),
    ],
)
def test_parse_model(text, expected):
    assert parse_model(text) == expected


@pytest.mark.parametrize("text", ["", "gpt-image-2", "a/b/c", "owner/bad slug", "../x"])
def test_parse_model_rejects(text):
    with pytest.raises(WiroError):
        parse_model(text)


def test_api_key_headers_and_masked_repr():
    creds = WiroCredentials(f"  {KEY} ")
    assert creds.headers() == {"x-api-key": KEY}
    assert KEY not in repr(creds) and KEY not in str(creds)
    assert not creds.uses_signature


def test_signature_headers():
    creds = WiroCredentials(KEY, SECRET)
    headers = creds.headers()
    expected = hmac.new(KEY.encode(), (SECRET + headers["x-nonce"]).encode(), hashlib.sha256).hexdigest()
    assert headers["x-api-key"] == KEY
    assert headers["x-signature"] == expected
    assert headers["x-nonce"].isdigit()
    assert SECRET not in repr(creds) and SECRET not in str(headers)


def test_empty_key_is_rejected():
    with pytest.raises(WiroError, match="WIRO_API_KEY"):
        WiroCredentials("   ")


def test_resolve_credentials(monkeypatch):
    with pytest.raises(WiroError, match="WIRO_API_KEY"):
        resolve_credentials(None)
    monkeypatch.setenv("WIRO_API_KEY", KEY)
    monkeypatch.setenv("WIRO_API_SECRET", SECRET)
    env = resolve_credentials(None)
    assert env.uses_signature and env.headers()["x-api-key"] == KEY
    node = WiroCredentials("from-node")
    assert resolve_credentials(node) is node


def make_client(fake_session, routes):
    session = fake_session(routes)
    return WiroClient(WiroCredentials(KEY), session=session), session


def test_post_sends_auth_and_user_agent(fake_session):
    client, session = make_client(fake_session, {"Tool/Detail": {"result": True, "tool": [{"id": "1"}]}})
    client.tool_detail("openai", "gpt-image-2")
    call = session.calls[0]
    assert call["url"] == "https://api.wiro.ai/v1/Tool/Detail"
    assert call["json"] == {"slugowner": "openai", "slugproject": "gpt-image-2"}
    assert call["headers"]["x-api-key"] == KEY
    assert call["headers"]["User-Agent"].startswith("comfyui-wiro/")


def test_result_false_becomes_error_with_hint(fake_session):
    client, _ = make_client(fake_session, {
        "Run/a/b": {"result": False, "errors": [{"code": 97, "message": "Insufficient balance"}]},
    })
    with pytest.raises(WiroError) as info:
        client.run("a", "b", build_run_request({"prompt": "x"}))
    assert "Insufficient balance" in str(info.value)
    assert "wiro.ai/panel/billing" in str(info.value)
    assert KEY not in str(info.value)


def test_auth_error_hint(fake_session):
    client, _ = make_client(fake_session, {
        "Run/a/b": FakeResponse({"result": False, "errors": [{"code": 0, "message": "Project authorization is not founded."}]}, 401),
    })
    with pytest.raises(WiroError, match="Check the API key"):
        client.run("a", "b", build_run_request({}))


def test_non_json_and_network_errors(fake_session):
    client, _ = make_client(fake_session, {"Task/Detail": FakeResponse("<html>", 502)})
    with pytest.raises(WiroError, match="HTTP 502"):
        client.task_detail("token")
    client, _ = make_client(fake_session, {"Task/Detail": requests.ConnectionError("boom")})
    with pytest.raises(WiroError, match="Could not reach"):
        client.task_detail("token")


def test_run_json_and_multipart(fake_session):
    ok = {"result": True, "taskid": "7", "socketaccesstoken": "tok"}
    client, session = make_client(fake_session, {"Run/openai/gpt-image-2": ok})
    client.run("openai", "gpt-image-2", build_run_request({"prompt": "fox", "samples": 2, "x": None}))
    assert session.calls[0]["json"] == {"prompt": "fox", "samples": 2}
    assert session.calls[0]["files"] is None

    upload = Upload("inputImage", "image-1.png", b"png", "image/png")
    client.run("openai", "gpt-image-2", build_run_request({"prompt": "fox"}, [upload], {"inputImage": "combinefileinput"}))
    call = session.calls[1]
    assert call["json"] is None
    assert call["data"] == [("prompt", "fox")]
    assert call["files"] == [("inputImage", ("image-1.png", b"png", "image/png"))]


def test_run_without_token_is_an_error(fake_session):
    client, _ = make_client(fake_session, {"Run/a/b": {"result": True, "taskid": "1"}})
    with pytest.raises(WiroError, match="no task token"):
        client.run("a", "b", build_run_request({}))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def detail_route(statuses):
    statuses = list(statuses)

    def answer(call):
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        if isinstance(status, Exception):
            return status
        return {"result": True, "tasklist": [{"id": "9", "socketaccesstoken": "tok", "status": status, "pexit": "0"}]}

    return answer


RUN = {"taskid": "9", "socketaccesstoken": "tok"}


def test_wait_until_postprocess_end(fake_session):
    clock = Clock()
    client, session = make_client(fake_session, {
        "Task/Detail": detail_route(["task_queue", "task_error", "task_start", "task_end", "task_postprocess_end"]),
    })
    seen = []
    task = wait_for_task(client, RUN, timeout=100, sleep=clock.sleep, clock=clock,
                         on_update=lambda t, e: seen.append(t["status"]))
    assert task["status"] == "task_postprocess_end"
    assert seen == ["task_queue", "task_error", "task_start", "task_end", "task_postprocess_end"]
    assert session.calls[0]["json"] == {"tasktoken": "tok"}


def test_wait_tolerates_a_few_poll_errors(fake_session):
    clock = Clock()
    client, _ = make_client(fake_session, {
        "Task/Detail": detail_route([requests.ConnectionError("x"), requests.ConnectionError("y"), "task_postprocess_end"]),
    })
    assert wait_for_task(client, RUN, timeout=100, sleep=clock.sleep, clock=clock)["status"] == "task_postprocess_end"

    client, _ = make_client(fake_session, {"Task/Detail": detail_route([requests.ConnectionError("x")])})
    with pytest.raises(WiroError):
        wait_for_task(client, RUN, timeout=100, sleep=clock.sleep, clock=clock, max_poll_errors=3)


def test_wait_timeout(fake_session):
    clock = Clock()
    client, _ = make_client(fake_session, {"Task/Detail": detail_route(["task_start"])})
    with pytest.raises(WiroTimeout, match="keeps running"):
        wait_for_task(client, RUN, timeout=10, sleep=clock.sleep, clock=clock)


def test_interrupt_cancels_a_queued_task(fake_session):
    client, session = make_client(fake_session, {"Task/Cancel": {"result": True}})
    with pytest.raises(WiroInterrupted):
        wait_for_task(client, RUN, timeout=10, should_stop=lambda: True)
    assert session.paths() == ["Task/Cancel"]
    assert session.calls[0]["json"] == {"taskid": "9"}


def test_interrupt_kills_a_running_task(fake_session):
    clock = Clock()
    stops = iter([False, True])
    client, session = make_client(fake_session, {
        "Task/Detail": detail_route(["task_start"]),
        "Task/Kill": {"result": True},
    })
    with pytest.raises(WiroInterrupted):
        wait_for_task(client, RUN, timeout=100, sleep=clock.sleep, clock=clock, should_stop=lambda: next(stops))
    assert session.paths() == ["Task/Detail", "Task/Kill"]
    assert session.calls[1]["json"] == {"socketaccesstoken": "tok"}


def test_stop_falls_back_to_kill(fake_session):
    client, session = make_client(fake_session, {
        "Task/Cancel": {"result": False, "errors": [{"code": 1, "message": "Task is not in a cancellable state."}]},
        "Task/Kill": {"result": True},
    })
    client.stop({"id": "9", "socketaccesstoken": "tok", "status": "task_accept"})
    assert session.paths() == ["Task/Cancel", "Task/Kill"]


def test_ensure_success():
    ensure_success({"status": "task_postprocess_end", "pexit": "0"})
    ensure_success({"status": "task_postprocess_end", "pexit": 0})
    with pytest.raises(WiroError, match=r"exit code 1\)\. CUDA out of memory"):
        ensure_success({"id": "5", "status": "task_postprocess_end", "pexit": "1", "debugerror": "CUDA out of memory"})
    with pytest.raises(WiroError, match="cancelled"):
        ensure_success({"id": "5", "status": "task_cancel", "pexit": "0"})
    long = "x" * 5000 + "END"
    with pytest.raises(WiroError) as info:
        ensure_success({"id": "5", "pexit": "2", "debugerror": long})
    assert str(info.value).endswith("END") and len(str(info.value)) < 800
