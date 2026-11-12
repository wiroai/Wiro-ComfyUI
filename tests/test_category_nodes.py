import os
import sys

import pytest
import torch

from wiro_comfyui import catalog, category_nodes, media, roles, runner
from wiro_comfyui.client import WiroError
from wiro_comfyui.results import read_task


def p(pid, ptype, **extra):
    return {"id": pid, "type": ptype, "label": extra.pop("label", pid), "required": extra.pop("required", False), **extra}


MODELS = [
    {"slug": "openai/gpt-image-2", "runs": 5, "categories": ["image-editing", "text-to-image"], "price": [], "params": [
        p("inputImage", "combinefileinput", max_files=2, label="Input Images"),
        p("inputImageMask", "combinefileinput", max_files=1, label="Mask Image (Edit)"),
        p("prompt", "textarea", default="", required=True),
        p("aspectRatio", "select", default="1:1", options=["1:1", "16:9"]),
        p("seed", "number", default="0", min=0, max=99, step=1),
        p("watermark", "checkbox", default="false"),
        p("model", "select", default="fast", options=["fast", "slow"]),
    ]},
    {"slug": "x/talker", "runs": 1, "categories": ["talking-head"], "price": [], "params": [
        p("inputImage", "fileinput", label="Portrait"),
        p("inputAudio", "fileinput", required=True, label="Speech"),
    ]},
]


@pytest.fixture(autouse=True)
def fake_catalog():
    catalog.set_for_tests(MODELS)
    yield
    catalog.set_for_tests([])


def spec(node):
    return next(s for s in roles.CATEGORY_SPECS if s["node"] == node)


def test_aliases_are_snake_case_and_unique():
    aliases = category_nodes.setting_aliases([{"id": "aspectRatio"}, {"id": "model"}, {"id": "aspect_ratio"}, {"id": "seed"}])
    assert aliases == {"aspectRatio": "aspect_ratio", "model": "model_setting", "aspect_ratio": "aspect_ratio_2", "seed": "seed"}


def test_build_request_maps_roles_and_settings():
    images = torch.rand(3, 4, 4, 3)
    mask = torch.rand(1, 4, 4, 3)
    model_value = {"model": "openai/gpt-image-2", "aspect_ratio": "16:9", "seed": 0, "watermark": True, "model_setting": "slow"}
    slug, values, uploads, file_types, plan = category_nodes.build_request(
        spec("WiroImageEdit"), model_value, {"text": "make it night", "images": images, "mask": mask})
    assert slug == "openai/gpt-image-2"
    # seed 0 is left to the model, the checkbox becomes a string, the reserved id maps back to "model"
    assert values == {"prompt": "make it night", "aspectRatio": "16:9", "watermark": "true", "model": "slow"}
    assert [u.field for u in uploads] == ["inputImage", "inputImage", "inputImageMask"]  # capped at max_files=2
    assert file_types == {"inputImage": "combinefileinput", "inputImageMask": "combinefileinput"}


def test_build_request_errors():
    with pytest.raises(WiroError, match="not in the Wiro AI catalog"):
        category_nodes.build_request(spec("WiroImageEdit"), {"model": "gone/model"}, {"text": "x"})
    with pytest.raises(WiroError, match="Connect the audio input"):
        category_nodes.build_request(spec("WiroLipsync"), {"model": "x/talker"}, {"image": torch.rand(1, 4, 4, 3)})


def test_build_request_audio_role():
    audio = {"waveform": torch.zeros(1, 1, 800), "sample_rate": 8000}
    _, values, uploads, file_types, _ = category_nodes.build_request(
        spec("WiroLipsync"), {"model": "x/talker"}, {"audio": audio, "image": torch.rand(1, 4, 4, 3)})
    assert values == {}
    assert [(u.field, u.mime) for u in uploads] == [("inputImage", "image/png"), ("inputAudio", "audio/wav")]
    assert file_types == {"inputImage": "fileinput", "inputAudio": "fileinput"}


def test_outputs_for(monkeypatch, png_bytes):
    result = read_task({"id": "1", "outputs": [{"name": "0.png", "contenttype": "image/png", "url": "https://cdn1.wiro.ai/0.png"}]})
    monkeypatch.setattr(media, "download", lambda url: png_bytes)
    (images,) = category_nodes.outputs_for("image", result)
    assert images.shape == (1, 6, 8, 3)
    images, mask = category_nodes.outputs_for("image_mask", result)
    assert mask.shape == (1, 6, 8) and float(mask.min()) == 1.0  # opaque PNG: full alpha
    with pytest.raises(WiroError, match="no video"):
        category_nodes.outputs_for("video", result)
    with pytest.raises(WiroError, match="no 3D file"):
        category_nodes.outputs_for("3d", result)


def test_media_kind_3d():
    assert media.media_kind("model/gltf-binary", "") == "3d"
    assert media.media_kind("application/octet-stream", "https://cdn1.wiro.ai/mesh.glb") == "3d"


# ------------------------------------------------- V3 schema (needs ComfyUI)

COMFYUI = os.environ.get("COMFYUI_PATH")


@pytest.fixture
def comfy_io():
    if not COMFYUI or not os.path.isdir(COMFYUI):
        pytest.skip("set COMFYUI_PATH to a ComfyUI checkout to test the V3 schemas")
    if COMFYUI not in sys.path:
        sys.path.insert(0, COMFYUI)
    io = category_nodes._io()
    if io is None:
        pytest.skip("this ComfyUI has no DynamicCombo")
    return io


def test_v3_schemas_build(comfy_io):
    classes, names = category_nodes.build_nodes()
    assert set(classes) == {s["node"] for s in roles.CATEGORY_SPECS}
    node = classes["WiroImageEdit"]
    info = node.GET_NODE_INFO_V1()
    model_spec = info["input"]["required"]["model"]
    assert model_spec[0] == "COMFY_DYNAMICCOMBO_V3"
    option = model_spec[1]["options"][0]
    assert option["key"] == "openai/gpt-image-2"
    assert set(option["inputs"]["required"]) == {"aspect_ratio", "seed", "watermark", "model_setting"}
    # No control widget inside a model menu (the frontend leaves stray copies behind).
    assert option["inputs"]["required"]["seed"][1]["control_after_generate"] is False
    assert info["input"]["required"]["prompt"][0] == "STRING"
    assert set(info["input"]["required"]) >= {"prompt", "model", "timeout", "force_rerun"}
    assert set(info["input"]["optional"]) == {"images", "mask", "wiro_auth"}
    assert info["output"] == ["IMAGE", "STRING"]
    assert classes["WiroRemoveBackground"].GET_NODE_INFO_V1()["output"][:2] == ["IMAGE", "MASK"]


def test_v3_execute(comfy_io, monkeypatch, fake_session, png_bytes):
    classes, _ = category_nodes.build_nodes()
    calls = {}

    def fake_run_task(credentials, slug, values, uploads, **kwargs):
        calls.update(slug=slug, values=values, uploads=[u.field for u in uploads])
        return read_task({"id": "9", "totalcost": "0.006", "outputs": [{"name": "0.png", "contenttype": "image/png", "url": "https://cdn1.wiro.ai/0.png"}]})

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    monkeypatch.setattr(media, "download", lambda url: png_bytes)
    monkeypatch.setenv("WIRO_API_KEY", "k")
    node = classes["WiroTextToImage"]
    out = node.execute(model={"model": "openai/gpt-image-2", "aspect_ratio": "1:1", "seed": 3, "watermark": False,
                              "model_setting": "fast"}, prompt="a fox")
    assert calls == {"slug": "openai/gpt-image-2", "uploads": [],
                     "values": {"prompt": "a fox", "aspectRatio": "1:1", "seed": 3, "watermark": "false", "model": "fast"}}
    assert out.args[0].shape == (1, 6, 8, 3) and out.args[1] == "https://cdn1.wiro.ai/0.png"
    assert out.ui == {"wiro_task": ["9"], "wiro_cost": ["0.006000"]}
