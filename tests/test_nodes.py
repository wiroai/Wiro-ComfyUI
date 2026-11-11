import os
import re

import pytest
import torch

from wiro_comfyui import keystore, media, nodes, runner
from wiro_comfyui.client import WiroCredentials, WiroError
from wiro_comfyui.version import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "node-test-key-0123456789"


def test_version_matches_pyproject():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        match = re.search(r'^version = "([^"]+)"', handle.read(), re.M)
    assert match and match.group(1) == __version__


def test_node_mappings():
    expected = {
        "WiroApiKey", "WiroRunModel", "WiroNanoBananaPro", "WiroNanoBanana2", "WiroGPTImage2",
        "WiroSeedream45", "WiroVeo31", "WiroKling3", "WiroSeedance1ProFast", "WiroElevenV4",
        "WiroElevenMusic2", "WiroElevenSpeechToText",
    }
    assert set(nodes.NODE_CLASS_MAPPINGS) == expected
    assert set(nodes.NODE_DISPLAY_NAME_MAPPINGS) == expected
    assert all(name.startswith("Wiro ") for name in nodes.NODE_DISPLAY_NAME_MAPPINGS.values())


@pytest.mark.parametrize("name", sorted(nodes.NODE_CLASS_MAPPINGS))
def test_input_specs_are_valid(name):
    cls = nodes.NODE_CLASS_MAPPINGS[name]
    spec = cls.INPUT_TYPES()
    assert set(spec) <= {"required", "optional", "hidden"}
    assert len(cls.RETURN_TYPES) == len(cls.RETURN_NAMES)
    assert callable(getattr(cls, cls.FUNCTION))
    assert cls.CATEGORY.startswith("Wiro AI")
    for section in ("required", "optional"):
        for input_name, (kind, options) in spec.get(section, {}).items():
            assert isinstance(options, dict)
            if isinstance(kind, list):
                assert options["default"] in kind, (input_name, options["default"], kind)
            elif kind in ("INT", "FLOAT"):
                assert options.get("min", -1e30) <= options["default"] <= options.get("max", 1e30)
            else:
                assert kind in ("STRING", "BOOLEAN", "IMAGE", "AUDIO", "VIDEO", "WIRO_AUTH"), kind


def test_widget_for():
    select = nodes.widget_for({"id": "shotType", "type": "select", "default": "", "options": ["customize", "intelligence"]})
    assert select == (["", "customize", "intelligence"], {"tooltip": "shotType", "display_name": "shot type", "default": ""})
    required = nodes.widget_for({"id": "mode", "type": "select", "default": "x", "options": ["std", "pro"], "required": True})
    assert required[1]["default"] == "std"
    seed = nodes.widget_for({"id": "seed", "type": "number", "default": "0", "min": 0, "max": 4294967295, "step": 1, "note": "Seed."})
    assert seed[0] == "INT" and seed[1]["control_after_generate"] == "fixed" and seed[1]["max"] == 4294967295
    stability = nodes.widget_for({"id": "stability", "type": "float", "default": "0.5", "min": 0, "max": 1, "step": 0.01, "advanced": True})
    assert stability[0] == "FLOAT" and stability[1]["default"] == 0.5 and stability[1]["advanced"] is True
    batch = nodes.widget_for({"id": "batchSize", "type": "float", "default": "8", "min": 0, "max": 20, "step": 1})
    assert batch[0] == "FLOAT"
    prompt = nodes.widget_for({"id": "prompt", "type": "textarea", "default": "a long sample", "label": "Prompt"})
    assert prompt == ("STRING", {"tooltip": "Prompt", "multiline": True, "default": ""})
    flag = nodes.widget_for({"id": "diarization", "type": "checkbox", "default": "true", "label": "Diarization"})
    assert flag[0] == "BOOLEAN" and flag[1]["default"] is True and "display_name" not in flag[1]
    samples = nodes.widget_for({"id": "samples", "type": "number", "default": "1", "min": 1, "max": 10, "step": 1,
                                "label": "Number of Images"})
    assert samples[1]["display_name"] == "number of images"
    audio = nodes.widget_for({"id": "inputAudio", "type": "fileinput", "label": "input-audio"})
    assert audio[0] == "AUDIO" and audio[1]["display_name"] == "input audio"
    fmt = nodes.widget_for({"id": "outputFormat", "type": "select", "label": "outputFormat", "default": "png", "options": ["png"]})
    assert fmt[1]["display_name"] == "output format"
    image = nodes.widget_for({"id": "inputImage2", "type": "combinefileinput", "label": "Last Frame"})
    assert image[0] == "IMAGE" and image[1]["display_name"] == "last frame"
    assert nodes.widget_for({"id": "inputAudio", "type": "fileinput"})[0] == "AUDIO"


def test_wiro_value():
    assert nodes.wiro_value({"id": "seed", "type": "number"}, 0) is None
    assert nodes.wiro_value({"id": "seed", "type": "number"}, 42) == 42
    assert nodes.wiro_value({"id": "diarization", "type": "checkbox"}, False) == "false"
    assert nodes.wiro_value({"id": "aspectRatio", "type": "select"}, "") is None
    assert nodes.wiro_value({"id": "ratio", "type": "select", "required": True}, "") == ""
    assert nodes.wiro_value({"id": "prompt", "type": "textarea"}, "fox") == "fox"


def test_api_key_node(monkeypatch):
    node = nodes.WiroApiKey()
    (creds,) = node.make(KEY)
    assert isinstance(creds, WiroCredentials) and creds.headers()["x-api-key"] == KEY
    (creds,) = node.make(keystore.put(KEY), keystore.put("sec"))
    assert creds.uses_signature
    with pytest.raises(WiroError, match="no longer available"):
        node.make("wiro-secret:" + "f" * 32)
    with pytest.raises(WiroError, match="WIRO_API_KEY"):
        node.make("")
    monkeypatch.setenv("WIRO_API_KEY", "from-env")
    (creds,) = node.make("")
    assert creds.headers()["x-api-key"] == "from-env"


class FakeWiro:
    """Stands in for the Wiro API: one task that finishes on the second poll."""

    def __init__(self, fake_session, outputs, tool_params=None):
        self.polls = 0
        task = {"id": "77", "socketaccesstoken": "tok", "pexit": "0", "totalcost": "0.006",
                "elapsedseconds": "4.0000", "outputs": outputs, "debugoutput": ""}

        def detail(call):
            self.polls += 1
            status = "task_postprocess_end" if self.polls >= 2 else "task_start"
            return {"result": True, "tasklist": [{**task, "status": status}]}

        self.session = fake_session({
            "Tool/Detail": {"result": True, "tool": [{"parameters": [{"items": tool_params or []}]}]},
            "Task/Detail": detail,
        })
        self.session.routes = {"/Run/": {"result": True, "taskid": "77", "socketaccesstoken": "tok"}, **self.session.routes}

    def install(self, monkeypatch):
        session = self.session
        original = runner.WiroClient.__init__

        def init(client, credentials, **kwargs):
            kwargs["session"] = session
            original(client, credentials, **kwargs)

        monkeypatch.setattr(runner.WiroClient, "__init__", init)
        monkeypatch.setattr("wiro_comfyui.client.time.sleep", lambda s: None)


def fake_run_route(session):
    # FakeSession matches suffixes; Run URLs end with the model slug.
    for call in session.calls:
        if "/Run/" in call["url"]:
            return call
    raise AssertionError("no Run call")


def test_run_model_node_image(monkeypatch, fake_session, png_bytes):
    wiro = FakeWiro(
        fake_session,
        [{"name": "0.png", "contenttype": "image/png", "url": "https://cdn1.wiro.ai/x/0.png"}],
        tool_params=[
            {"id": "prompt", "type": "textarea", "required": True, "defaultvalue": "sample prompt"},
            {"id": "quality", "type": "select", "required": True, "defaultvalue": "medium"},
            {"id": "samples", "type": "number", "required": True, "defaultvalue": "1"},
            {"id": "background", "type": "select", "required": False, "defaultvalue": "auto"},
        ],
    )
    wiro.session.routes = {"Run/openai/gpt-image-2": {"result": True, "taskid": "77", "socketaccesstoken": "tok"},
                           **wiro.session.routes}
    wiro.install(monkeypatch)
    monkeypatch.setattr(media, "download", lambda url: png_bytes)
    monkeypatch.setenv("WIRO_API_KEY", KEY)

    images, video, audio, text, urls = nodes.WiroRunModel().run(
        model="openai/gpt-image-2", prompt="a red fox", parameters='{"quality": "low"}')
    assert images.shape == (1, 6, 8, 3)
    assert video is None and audio is None  # ExecutionBlocker outside ComfyUI
    assert text == "" and urls == "https://cdn1.wiro.ai/x/0.png"
    run_call = fake_run_route(wiro.session)
    # samples is required on the model and was left out, so its default is sent.
    assert run_call["json"] == {"quality": "low", "prompt": "a red fox", "samples": 1}


def test_run_model_node_uploads_image_to_detected_field(monkeypatch, fake_session, png_bytes):
    wiro = FakeWiro(
        fake_session,
        [{"name": "0.mp4", "contenttype": "video/mp4", "url": "https://cdn1.wiro.ai/x/0.mp4"}],
        tool_params=[{"id": "inputImage", "type": "combinefileinput"}, {"id": "inputImage2", "type": "combinefileinput"}],
    )
    wiro.session.routes = {"Run/klingai/kling-v3": {"result": True, "taskid": "77", "socketaccesstoken": "tok"},
                           **wiro.session.routes}
    wiro.install(monkeypatch)
    videos = []
    monkeypatch.setattr(media, "download", lambda url: b"mp4-bytes")
    monkeypatch.setattr(media, "video_from_bytes", lambda blob, name: videos.append((blob, name)) or "VIDEO")

    first, last = torch.rand(1, 4, 4, 3), torch.rand(1, 4, 4, 3)
    images, video, audio, text, urls = nodes.WiroRunModel().run(
        model="klingai/kling-v3", prompt="", parameters='{"mode": "std"}',
        wiro_auth=WiroCredentials(KEY), image=first, image_2=last)
    assert video == "VIDEO" and videos == [(b"mp4-bytes", "0.mp4")]
    assert images is None
    run_call = fake_run_route(wiro.session)
    assert run_call["data"] == [("mode", "std")]
    assert [f[0] for f in run_call["files"]] == ["inputImage", "inputImage2"]


def test_run_model_node_rejects_bad_json(monkeypatch):
    monkeypatch.setenv("WIRO_API_KEY", KEY)
    with pytest.raises(WiroError, match="not valid JSON"):
        nodes.WiroRunModel().run(model="a/b", prompt="", parameters="{oops")


def test_model_node_request(monkeypatch, fake_session, png_bytes):
    wiro = FakeWiro(fake_session, [{"name": "0.png", "contenttype": "image/png", "url": "https://cdn1.wiro.ai/x/0.png"}])
    wiro.session.routes = {"Run/openai/gpt-image-2": {"result": True, "taskid": "77", "socketaccesstoken": "tok"},
                           **wiro.session.routes}
    wiro.install(monkeypatch)
    monkeypatch.setattr(media, "download", lambda url: png_bytes)

    cls = nodes.NODE_CLASS_MAPPINGS["WiroGPTImage2"]
    spec = cls.INPUT_TYPES()
    widgets = {name: options.get("default") for name, (_, options) in spec["required"].items()}
    widgets["prompt"] = "a lighthouse"
    images, urls = cls().run(wiro_auth=WiroCredentials(KEY), **widgets)
    assert images.shape[0] == 1
    body = fake_run_route(wiro.session)["json"]
    assert body["prompt"] == "a lighthouse"
    assert body["resolution"] == "1k" and body["ratio"] == "1:1" and body["quality"] == "low"
    assert "inputImage" not in body

    wiro.session.calls.clear()
    cls().run(wiro_auth=WiroCredentials(KEY), inputImage=torch.rand(3, 4, 4, 3), **widgets)
    run_call = fake_run_route(wiro.session)
    assert [f[0] for f in run_call["files"]] == ["inputImage"] * 3
    assert not any(k == "inputImageUrl" for k, _ in run_call["data"])


def test_model_node_failure_is_reported(monkeypatch, fake_session):
    wiro = FakeWiro(fake_session, [])
    wiro.session.routes = {"Run/elevenlabs/eleven-v4": {"result": True, "taskid": "77", "socketaccesstoken": "tok"},
                           **wiro.session.routes}
    wiro.install(monkeypatch)
    cls = nodes.NODE_CLASS_MAPPINGS["WiroElevenV4"]
    widgets = {name: options.get("default") for name, (_, options) in cls.INPUT_TYPES()["required"].items()}
    with pytest.raises(WiroError, match="returned no audio"):
        cls().run(wiro_auth=WiroCredentials(KEY), **dict(widgets, prompt="Hello"))


def test_force_rerun():
    assert nodes.WiroRunModel.IS_CHANGED(force_rerun=False) == ""
    assert isinstance(nodes.WiroRunModel.IS_CHANGED(force_rerun=True), float)
    cls = nodes.NODE_CLASS_MAPPINGS["WiroVeo31"]
    assert cls.IS_CHANGED(force_rerun=False, prompt="x") == ""
