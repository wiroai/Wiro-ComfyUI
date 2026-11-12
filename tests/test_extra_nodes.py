import base64
import json

import pytest
import torch

from helpers import FakeResponse, blocked
from wiro_comfyui import catalog, extra_nodes, media
from wiro_comfyui.client import WiroCredentials, WiroError
from wiro_comfyui.server_hooks import price_tables

KEY = "llm-test-key"


class LLMSession:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.answer


def test_llm_messages_and_answer():
    assert extra_nodes.build_llm_messages("hi", "", []) == [{"role": "user", "content": "hi"}]
    messages = extra_nodes.build_llm_messages("describe", "be brief", [b"png"])
    assert messages[0] == {"role": "system", "content": "be brief"}
    parts = messages[1]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"png").decode()
    assert extra_nodes.llm_answer({"choices": [{"message": {"content": " ok "}}]}) == "ok"
    assert extra_nodes.llm_answer({"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}]}) == "ab"
    with pytest.raises(WiroError):
        extra_nodes.llm_answer({"choices": []})


def test_call_llm_auth_and_errors():
    creds = WiroCredentials(KEY, "sec")
    session = LLMSession(FakeResponse({"choices": [{"message": {"content": "x"}}], "usage": {"cost": 0.0001}}))
    extra_nodes.call_llm(creds, {"model": "m"}, session=session)
    assert session.calls[0]["url"] == "https://llm.wiro.ai/v1/chat/completions"
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}:sec"
    bad = LLMSession(FakeResponse({"error": {"message": "Invalid API key"}}, 401))
    with pytest.raises(WiroError, match="Invalid API key. Check the API key"):
        extra_nodes.call_llm(WiroCredentials(KEY), {"model": "m"}, session=bad)


def test_llm_node(monkeypatch):
    catalog.set_for_tests([], [{"id": "openai/gpt-5-nano", "image": False}, {"id": "google/gemini", "image": True}])
    assert extra_nodes.llm_choices()[0] == "openai/gpt-5-nano"
    seen = {}

    def fake_call(credentials, payload, **kwargs):
        seen.update(payload)
        return {"id": "chatcmpl-1", "choices": [{"message": {"content": "A red fox."}}], "usage": {"cost": 0.00012}}

    monkeypatch.setattr(extra_nodes, "call_llm", fake_call)
    out = extra_nodes.WiroLLM().run("openai/gpt-5-nano", "Describe a fox", "One sentence.",
                                    wiro_auth=WiroCredentials(KEY), max_tokens=64, temperature=0.2)
    assert out["result"] == ("A red fox.",)
    assert out["ui"] == {"wiro_task": ["chatcmpl-1"], "wiro_cost": ["0.000120"]}
    assert seen["max_tokens"] == 64 and seen["temperature"] == 0.2
    assert seen["messages"][0]["role"] == "system"
    with pytest.raises(WiroError, match="does not read images"):
        extra_nodes.WiroLLM().run("openai/gpt-5-nano", "x", image=torch.rand(1, 2, 2, 3), wiro_auth=WiroCredentials(KEY))
    catalog.set_for_tests([], [])


def test_workflow_slug_and_fields():
    assert extra_nodes.parse_workflow_slug("product-ad-a1B2c") == "product-ad-a1B2c"
    assert extra_nodes.parse_workflow_slug("https://wiro.ai/workflows/product-ad-a1B2c?x=1") == "product-ad-a1B2c"
    with pytest.raises(WiroError):
        extra_nodes.parse_workflow_slug("not a slug")
    detail = {"contents": json.dumps({"schema": {"input": {
        "prompt": {"ui": {"field": "textarea"}}, "photo": {"ui": {"field": "image"}}, "voice": {"ui": {"field": "audio"}}}}})}
    fields = extra_nodes.workflow_fields(detail)
    assert extra_nodes.pick_workflow_field(fields, "image") == "photo"
    assert extra_nodes.pick_workflow_field(fields, "audio", "voice") == "voice"
    with pytest.raises(WiroError, match="no video input"):
        extra_nodes.pick_workflow_field(fields, "video")
    with pytest.raises(WiroError, match='no input "nope"'):
        extra_nodes.pick_workflow_field(fields, "image", "nope")


RUN = {
    "workflowtoken": "tok", "status": "completed", "chargedcost": "0.0062",
    "contents": {"output": {"image": "$node-edit.outputs", "caption": "$node-cap.outputs", "pick": "$node-split.text2"}},
}
NODES = [
    {"nodeId": "node-edit", "status": "completed", "outputs": [{"url": "https://cdn1.wiro.ai/a.png", "name": "a.png", "contenttype": "image/png"}]},
    {"nodeId": "node-cap", "status": "completed", "outputs": [{"contenttype": "raw", "content": {"answer": ["A fox ", "at night."]}}]},
    {"nodeId": "node-split", "status": "completed", "outputs": {"text1": ["one"], "text2": ["two"]}},
    {"nodeId": "node-unused", "status": "completed", "outputs": [{"url": "https://cdn1.wiro.ai/zzz.mp4", "contenttype": "video/mp4"}]},
]


def test_collect_workflow_outputs():
    result = extra_nodes.collect_workflow_outputs(RUN, NODES)
    assert [f.url for f in result.files] == ["https://cdn1.wiro.ai/a.png"]
    assert result.llm_text() == "A fox at night.\n\ntwo"
    assert extra_nodes._cost(RUN) == 0.0062


def test_run_workflow_node(monkeypatch, fake_session, png_bytes):
    detail = {"result": True, "workflow": [{"contents": {"schema": {"input": {
        "prompt": {"ui": {"field": "textarea"}}, "photo": {"ui": {"field": "image"}}}}}}]}
    states = iter([
        {"result": True, "workflowrun": {"status": "running"}, "workflowrunnodes": [{"nodeId": "node-edit", "status": "running"}]},
        {"result": True, "workflowrun": RUN, "workflowrunnodes": NODES},
    ])
    session = fake_session({
        "Workflow/Detail": detail,
        "File/Upload": {"result": True, "list": [{"url": "https://cdn1.wiro.ai/up/photo.png"}]},
        "Workflow/Run": {"result": True, "workflowtoken": "tok"},
        "Workflow/RunDetail": lambda call: next(states),
    })
    original = extra_nodes.WiroClient.__init__
    monkeypatch.setattr(extra_nodes.WiroClient, "__init__", lambda c, cred, **kw: original(c, cred, session=session))
    monkeypatch.setattr(extra_nodes.time, "sleep", lambda s: None)
    monkeypatch.setattr(media, "download", lambda url: png_bytes)
    out = extra_nodes.WiroRunWorkflow().run("https://wiro.ai/workflows/test-a1B2c", '{"prompt": "night"}',
                                           wiro_auth=WiroCredentials(KEY), image=torch.rand(1, 4, 4, 3))
    images, video, audio, text, urls = out["result"]
    assert images.shape == (1, 6, 8, 3) and blocked(video) and blocked(audio)
    assert text == "A fox at night.\n\ntwo" and urls == "https://cdn1.wiro.ai/a.png"
    assert out["ui"] == {"wiro_task": ["tok"], "wiro_cost": ["0.006200"]}
    run_call = next(c for c in session.calls if c["url"].endswith("Workflow/Run"))
    assert run_call["json"] == {"slug": "test-a1B2c", "input": {"prompt": "night", "photo": "https://cdn1.wiro.ai/up/photo.png"}}
    upload = next(c for c in session.calls if c["url"].endswith("File/Upload"))
    assert upload["files"][0][0] == "file" and upload["files"][0][1][2] == "image/png"


def test_run_workflow_failure(monkeypatch, fake_session):
    session = fake_session({
        "Workflow/Detail": {"result": True, "workflow": [{"contents": {"schema": {"input": {}}}}]},
        "Workflow/Run": {"result": True, "workflowtoken": "tok"},
        "Workflow/RunDetail": {"result": True, "workflowrun": {"status": "failed"}, "workflowrunnodes": [
            {"nodeId": "node-edit", "tool": "openai/gpt-image-2", "status": "failed", "error": "Request parameter [prompt] required"}]},
    })
    original = extra_nodes.WiroClient.__init__
    monkeypatch.setattr(extra_nodes.WiroClient, "__init__", lambda c, cred, **kw: original(c, cred, session=session))
    with pytest.raises(WiroError, match=r"ended as failed at step node-edit \(openai/gpt-image-2\): Request parameter"):
        extra_nodes.WiroRunWorkflow().run("test-a1B2c", "{}", wiro_auth=WiroCredentials(KEY))


def test_price_tables():
    tables = price_tables(
        [{"slug": "a/b", "price": [{"inputs": {}, "price": 0.01, "method": "cpr"}], "cps": None, "approx": None}],
        [{"id": "m", "prompt": 1e-7, "completion": 2e-7}],
        {"WiroGPTImage2": "openai/gpt-image-2"},
    )
    assert tables == {
        "models": {"a/b": {"price": [{"inputs": {}, "price": 0.01, "method": "cpr"}], "cps": None, "approx": None}},
        "llm": {"m": {"prompt": 1e-7, "completion": 2e-7}},
        "fixed": {"WiroGPTImage2": "openai/gpt-image-2"},
    }
