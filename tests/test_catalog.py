import gzip
import json

from wiro_comfyui import catalog, roles

TOOL = {
    "cleanslugowner": "openai",
    "cleanslugproject": "gpt-image-2",
    "title": "GPT Image 2 \u2014 edits",
    "categories": ["text-to-image", "image-editing"],
    "taskstat": {"runcount": "174626"},
    "cps": "0.000000000000",
    "quegroupcps": "0",
    "approximatelycost": 0,
    "dynamicprice": json.dumps([{"inputs": {"quality": "low"}, "price": 0.006, "priceMethod": "cpr"}]),
    "parameters": [{"items": [
        {"id": "inputImage", "type": "combinefileinput", "maxinputlenght": 16, "label": "Input Images"},
        {"id": "prompt", "type": "textarea", "defaultvalue": "a long sample prompt \u2014 with a dash", "required": True},
        {"id": "quality", "type": "select", "defaultvalue": "low", "options": [{"value": "low"}, {"value": "high"}]},
        {"id": "style", "type": "selectwithcover", "defaultvalue": "a", "options": [{"value": "a", "cover": "x.png"}]},
        {"id": "samples", "type": "number", "defaultvalue": "1", "minvalue": "1", "maxvalue": "10", "incrementby": "1"},
        {"id": "tags", "type": "multiselect", "defaultvalue": ""},
    ]}],
}


def test_compact_model():
    model = catalog.compact_model(TOOL)
    assert model["slug"] == "openai/gpt-image-2"
    assert "\u2014" not in model["title"]
    assert model["runs"] == 174626
    assert model["cps"] is None and model["approx"] is None
    assert model["price"] == [{"inputs": {"quality": "low"}, "price": 0.006, "method": "cpr"}]
    params = {p["id"]: p for p in model["params"]}
    assert set(params) == {"inputImage", "prompt", "quality", "style", "samples"}  # multiselect dropped
    assert params["inputImage"]["max_files"] == 16
    assert params["prompt"]["default"] == ""  # sample prompts are not kept
    assert params["style"]["type"] == "select" and params["style"]["options"] == ["a"]
    assert params["samples"]["min"] == 1 and params["samples"]["max"] == 10


def test_gpu_priced_model_and_realtime_skip():
    gpu = dict(TOOL, dynamicprice="", cps="0", quegroupcps="0.0006", approximatelycost=0.0012082)
    model = catalog.compact_model(gpu)
    assert model["price"] == [] and model["cps"] == 0.0006 and model["approx"] == 0.001208
    assert catalog.compact_model(dict(TOOL, categories=["realtime-tts"])) is None
    assert catalog.compact_model({"cleanslugowner": "x"}) is None


def test_compact_llm_model():
    model = catalog.compact_llm_model({
        "id": "openai/gpt-5-nano", "display_name": "(Wiro) GPT 5 Nano",
        "capabilities": {"endpoints": ["chat", "responses"], "input_modalities": ["text", "image"]},
        "pricing": {"prompt": "0.00000005", "completion": "0.0000004"}, "max_tokens": 128000,
    })
    assert model == {"id": "openai/gpt-5-nano", "name": "(Wiro) GPT 5 Nano", "image": True,
                     "endpoints": ["chat", "responses"], "prompt": 5e-08, "completion": 4e-07, "max_tokens": 128000}


def test_snapshot_ships_with_the_pack():
    models = json.load(gzip.open(catalog.SNAPSHOT, "rt", encoding="utf-8"))
    assert len(models) > 300
    for spec in roles.CATEGORY_SPECS:
        picked = roles.models_for(spec, models)
        assert picked, spec["node"]
        assert picked[0][0]["slug"] == spec["default"], spec["node"]
    llm = json.load(open(catalog.LLM_SNAPSHOT, encoding="utf-8"))
    assert any(m["id"] == "openai/gpt-5-nano" for m in llm)


def test_start_uses_the_snapshot_offline(monkeypatch):
    monkeypatch.setattr(catalog, "_started", False)
    monkeypatch.setattr(catalog, "_cache_dir", lambda: None)
    started = []
    monkeypatch.setattr(catalog.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: started.append(kw)})())
    catalog.start()
    assert len(catalog.models()) > 300 and catalog.llm_models()
    assert catalog.model("openai/gpt-image-2") is not None
    assert started  # a refresh was scheduled because the snapshot has no age
    catalog.start()  # idempotent
    assert len(started) == 1
