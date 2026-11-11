import pytest

from wiro_comfyui.payload import (
    Upload,
    required_defaults_from_tool,
    build_run_request,
    file_params_from_tool,
    form_value,
    parse_parameters,
    pick_file_field,
    url_field,
)


def upload(field, name="f.png"):
    return Upload(field, name, b"data", "image/png")


def test_json_body_without_files():
    request = build_run_request({"prompt": "fox", "samples": 2, "seed": None})
    assert request.json_body == {"prompt": "fox", "samples": 2}
    assert not request.is_multipart


def test_url_field_names():
    assert url_field("inputImage", "fileinput") == "inputImageUrl"
    assert url_field("inputDocumentMultiple", "multifileinput") == "inputDocumentUrlMultiple"
    assert url_field("inputImage", "combinefileinput") is None
    assert url_field("inputImage", None) is None


def test_fileinput_upload_sends_empty_url_field():
    request = build_run_request(
        {"inputAudioUrl": "https://example.com/a.mp3", "inputAudio": "", "language": "en"},
        [Upload("inputAudio", "audio.wav", b"wav", "audio/wav")],
        {"inputAudio": "fileinput"},
    )
    assert request.form_fields == [("language", "en"), ("inputAudioUrl", "")]
    assert request.files == [("inputAudio", ("audio.wav", b"wav", "audio/wav"))]


def test_multifileinput_upload():
    request = build_run_request(
        {"prompt": "summarize"},
        [upload("inputDocumentMultiple", "a.pdf"), upload("inputDocumentMultiple", "b.pdf")],
        {"inputDocumentMultiple": "multifileinput"},
    )
    assert ("inputDocumentUrlMultiple", "") in request.form_fields
    assert [f[0] for f in request.files] == ["inputDocumentMultiple", "inputDocumentMultiple"]


def test_combinefileinput_mixes_files_and_urls():
    request = build_run_request(
        {"prompt": "dress", "inputImageClothes": ["https://example.com/a.jpg", ""]},
        [upload("inputImageClothes")],
        {"inputImageClothes": "combinefileinput"},
    )
    assert request.form_fields == [("prompt", "dress"), ("inputImageClothes", "https://example.com/a.jpg")]
    assert request.files[0][0] == "inputImageClothes"


def test_url_for_another_fileinput_gets_an_empty_file_field():
    request = build_run_request(
        {"inputImageUrl": "https://example.com/face.jpg"},
        [Upload("inputAudio", "audio.wav", b"wav", "audio/wav")],
        {"inputImage": "fileinput", "inputAudio": "fileinput"},
    )
    fields = dict(request.form_fields)
    assert fields["inputImageUrl"] == "https://example.com/face.jpg"
    assert fields["inputImage"] == ""
    assert fields["inputAudioUrl"] == ""


def test_unknown_field_type_is_treated_as_combine():
    request = build_run_request({"prompt": "x"}, [upload("inputImage")], {})
    assert request.form_fields == [("prompt", "x")]


@pytest.mark.parametrize(
    "value, expected",
    [(True, "true"), (False, "false"), (3, "3"), (0.5, "0.5"), ({"a": 1}, '{"a": 1}'), ([1, 2], "[1, 2]"), ("x", "x")],
)
def test_form_value(value, expected):
    assert form_value(value) == expected


def test_multipart_values_are_strings():
    request = build_run_request({"samples": 2, "flag": True, "tools": [{"type": "function"}]}, [upload("inputImage")])
    assert request.form_fields == [("samples", "2"), ("flag", "true"), ("tools", '[{"type": "function"}]')]


def test_parse_parameters():
    assert parse_parameters("") == {}
    assert parse_parameters('  {"a": 1}  ') == {"a": 1}
    with pytest.raises(ValueError, match="line 1, column"):
        parse_parameters('{"a": }')
    with pytest.raises(ValueError, match="JSON object"):
        parse_parameters("[1, 2]")


TOOL = {
    "parameters": [
        {"items": [
            {"id": "inputImage", "type": "combinefileinput"},
            {"id": "prompt", "type": "textarea"},
            {"id": "inputImageMask", "type": "combinefileinput"},
            {"id": "resolution", "type": "select"},
        ]},
        {"items": [{"id": "inputAudio", "type": "fileinput"}]},
    ]
}


def test_file_params_from_tool():
    assert file_params_from_tool(TOOL) == {
        "inputImage": "combinefileinput",
        "inputImageMask": "combinefileinput",
        "inputAudio": "fileinput",
    }
    assert file_params_from_tool({}) == {}


def test_pick_file_field_auto():
    params = file_params_from_tool(TOOL)
    assert pick_file_field(params, "image") == "inputImage"
    # No second image parameter, but inputImage takes several files.
    assert pick_file_field(params, "image", index=1) == "inputImage"
    assert pick_file_field(params, "audio") == "inputAudio"
    with pytest.raises(ValueError, match="no video input"):
        pick_file_field(params, "video", model="x/y")


def test_pick_file_field_two_frames():
    params = {"inputImage": "combinefileinput", "inputImage2": "combinefileinput"}
    assert pick_file_field(params, "image") == "inputImage"
    assert pick_file_field(params, "image", index=1) == "inputImage2"


def test_pick_file_field_explicit_and_fallbacks():
    params = {"inputAll": "combinefileinput"}
    assert pick_file_field(params, "image") == "inputAll"
    assert pick_file_field(params, "audio", explicit="inputAll") == "inputAll"
    with pytest.raises(ValueError, match="no file parameter"):
        pick_file_field(params, "image", explicit="inputImage")
    # Tool/Detail unavailable: documented default names.
    assert pick_file_field(None, "image") == "inputImage"
    assert pick_file_field(None, "image", index=1) == "inputImage2"
    assert pick_file_field(None, "video") == "inputVideo"
    assert pick_file_field(None, "image", explicit="inputImageHuman") == "inputImageHuman"


def test_upload_repr_hides_bytes():
    assert "data" not in repr(Upload("f", "n", b"data", "x"))


def test_required_defaults_from_tool():
    tool = {"parameters": [{"items": [
        {"id": "prompt", "type": "textarea", "required": True, "defaultvalue": "a long sample prompt"},
        {"id": "resolution", "type": "select", "required": True, "defaultvalue": "1k"},
        {"id": "samples", "type": "number", "required": True, "defaultvalue": "1"},
        {"id": "scale", "type": "float", "required": True, "defaultvalue": "1"},
        {"id": "batchSize", "type": "number", "required": True, "defaultvalue": "8.5"},
        {"id": "diarization", "type": "checkbox", "required": True, "defaultvalue": "true"},
        {"id": "ratio", "type": "select", "required": True, "defaultvalue": ""},
        {"id": "background", "type": "select", "required": False, "defaultvalue": "auto"},
        {"id": "inputImage", "type": "combinefileinput", "required": True, "defaultvalue": "https://x/y.jpg"},
        {"id": "seed", "type": "number", "required": True, "defaultvalue": "abc"},
    ]}]}
    assert required_defaults_from_tool(tool) == {
        "resolution": "1k", "samples": 1, "scale": 1.0, "batchSize": 8.5, "diarization": "true",
    }
