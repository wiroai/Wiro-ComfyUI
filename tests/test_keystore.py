import copy
import json

import pytest

from wiro_comfyui import keystore

KEY = "live-looking-key-0123456789abcdef"


def test_put_and_resolve():
    ref = keystore.put(f"  {KEY}  ")
    assert keystore.is_ref(ref)
    assert KEY not in ref
    assert keystore.put(KEY) == ref  # stable, so ComfyUI's cache keeps working
    assert keystore.resolve(ref) == KEY
    assert keystore.resolve(KEY) == KEY  # plain values pass through
    assert keystore.resolve("wiro-secret:" + "0" * 32) is None  # unknown reference
    assert keystore.put("") == "" and keystore.put(ref) == ref
    assert keystore.resolve(None) is None


def test_put_rejects_huge_values():
    with pytest.raises(ValueError):
        keystore.put("x" * 5000)


def prompt_with_key(api_key, api_secret=""):
    return {
        "client_id": "abc",
        "prompt": {
            "1": {"class_type": "WiroApiKey", "inputs": {"api_key": api_key, "api_secret": api_secret}},
            "2": {"class_type": "WiroRunModel", "inputs": {"model": "openai/gpt-image-2", "wiro_auth": ["1", 0]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": KEY}},
        },
        "extra_data": {
            "extra_pnginfo": {
                "workflow": {
                    "nodes": [
                        {"id": 1, "type": "WiroApiKey", "widgets_values": [KEY, "secret"],
                         "widgets_values_named": {"api_key": KEY, "api_secret": "my-secret"}},
                        {"id": 2, "type": "WiroRunModel", "widgets_values": ["openai/gpt-image-2", "fox", "{}"]},
                    ],
                    "definitions": {"subgraphs": [{"nodes": [{"id": 7, "type": "WiroApiKey", "widgets_values": [KEY, ""]}]}]},
                }
            }
        },
    }


def test_scrub_prompt_removes_raw_keys_everywhere_it_matters():
    data = prompt_with_key(KEY, "my-secret")
    out = keystore.scrub_prompt(data)
    assert out is data
    inputs = data["prompt"]["1"]["inputs"]
    assert keystore.is_ref(inputs["api_key"]) and keystore.resolve(inputs["api_key"]) == KEY
    assert keystore.resolve(inputs["api_secret"]) == "my-secret"
    # Other nodes are left alone (only the key node holds secrets).
    assert data["prompt"]["3"]["inputs"]["text"] == KEY
    assert data["prompt"]["2"]["inputs"]["wiro_auth"] == ["1", 0]
    workflow = json.dumps(data["extra_data"]["extra_pnginfo"]["workflow"])
    assert KEY not in workflow and "my-secret" not in workflow
    assert "openai/gpt-image-2" in workflow


def test_scrub_prompt_keeps_references_and_links():
    ref = keystore.put(KEY)
    data = prompt_with_key(ref)
    data["prompt"]["1"]["inputs"]["api_secret"] = ["5", 0]  # linked from another node
    before = copy.deepcopy(data["prompt"])
    keystore.scrub_prompt(data)
    assert data["prompt"] == before


@pytest.mark.parametrize("junk", [None, [], "text", {"prompt": None}, {"prompt": {"1": None}},
                                  {"prompt": {"1": {"class_type": "WiroApiKey"}}},
                                  {"extra_data": {"extra_pnginfo": "x"}}])
def test_scrub_prompt_never_raises(junk):
    assert keystore.scrub_prompt(junk) is junk


def test_scrub_prompt_drops_values_that_cannot_be_keys():
    data = prompt_with_key("x" * 5000)
    keystore.scrub_prompt(data)
    assert data["prompt"]["1"]["inputs"]["api_key"] == ""
