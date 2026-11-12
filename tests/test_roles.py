from wiro_comfyui import roles


def p(pid, ptype="combinefileinput", **extra):
    return {"id": pid, "type": ptype, "label": extra.pop("label", pid), **extra}


def spec(node):
    return next(s for s in roles.CATEGORY_SPECS if s["node"] == node)


def test_file_kind():
    assert roles.file_kind(p("inputImage")) == "image"
    assert roles.file_kind(p("inputAudio", "fileinput")) == "audio"
    assert roles.file_kind(p("inputVideo")) == "video"
    assert roles.file_kind(p("inputImageMask")) == "image"
    assert roles.file_kind(p("inputDocumentMultiple", "multifileinput")) is None


def test_text_param():
    assert roles.text_param([p("negativePrompt", "textarea"), p("prompt", "textarea")])["id"] == "prompt"
    assert roles.text_param([p("text", "text")])["id"] == "text"
    assert roles.text_param([p("negativePrompt", "textarea"), p("caption", "textarea")])["id"] == "caption"
    assert roles.text_param([p("negativePrompt", "textarea")]) is None


def test_find_roles_frames_and_mask():
    params = [p("inputImage", label="First Frame"), p("inputImage2", label="Last Frame"), p("inputImage3", label="Reference Images"),
              p("inputImageMask", label="Mask"), p("prompt", "textarea"), p("inputAudio", "fileinput")]
    found = roles.find_roles(params)
    assert found["first_frame"]["id"] == "inputImage"
    assert found["last_frame"]["id"] == "inputImage2"
    assert found["mask"]["id"] == "inputImageMask"
    assert found["audio"]["id"] == "inputAudio"
    assert found["text"]["id"] == "prompt"


def test_plan_model_required_roles_and_extras():
    model = {"slug": "x/v", "params": [p("inputImage", label="First Frame"), p("inputImageLast", label="Last frame"),
                                       p("inputImage3", label="Reference"), p("prompt", "textarea"),
                                       p("resolution", "select", options=["480p"], default="480p")]}
    plan = roles.plan_model(spec("WiroImageToVideo"), model)
    assert {k: v["id"] for k, v in plan["roles"].items()} == {"text": "prompt", "first_frame": "inputImage", "last_frame": "inputImageLast"}
    assert [x["id"] for x in plan["settings"]] == ["resolution"]
    assert [x["id"] for x in plan["extra_files"]] == ["inputImage3"]
    # No image: cannot animate.
    assert roles.plan_model(spec("WiroImageToVideo"), {"params": [p("prompt", "textarea")]}) is None
    # A required document input the node cannot provide.
    doc = {"params": [p("prompt", "textarea"), p("inputDocument", "fileinput", required=True)]}
    assert roles.plan_model(spec("WiroTextToImage"), doc) is None


def test_models_for_orders_default_first():
    catalog = [
        {"slug": "a/popular", "runs": 900, "categories": ["text-to-image"], "params": [p("prompt", "textarea")]},
        {"slug": "google/nano-banana", "runs": 10, "categories": ["text-to-image"], "params": [p("prompt", "textarea")]},
        {"slug": "b/other", "runs": 50, "categories": ["text-to-image"], "params": [p("prompt", "textarea")]},
        {"slug": "c/no-prompt", "runs": 99999, "categories": ["text-to-image"], "params": [p("inputImage")]},
        {"slug": "d/video", "runs": 1, "categories": ["text-to-video"], "params": [p("prompt", "textarea")]},
    ]
    slugs = [m["slug"] for m, _ in roles.models_for(spec("WiroTextToImage"), catalog)]
    assert slugs == ["google/nano-banana", "a/popular", "b/other"]
