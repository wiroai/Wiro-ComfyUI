from wiro_comfyui import media, runner
from wiro_comfyui.results import read_task

IMAGE_TASK = {
    "id": "11",
    "status": "task_postprocess_end",
    "pexit": "0",
    "totalcost": "0.006000000000",
    "debugoutput": "step 1/4\nstep 4/4",
    "outputs": [
        {"name": "0.png", "contenttype": "image/png", "url": "https://cdn1.wiro.ai/a/0.png"},
        {"name": "1.png", "contenttype": "image/png", "url": "https://cdn1.wiro.ai/a/1.png"},
    ],
}

LLM_TASK = {
    "id": "12",
    "pexit": "0",
    "debugoutput": "Hello! How can I help you today?",
    "outputs": [{
        "contenttype": "raw",
        "content": {
            "raw": "Hello! How can I help you today?",
            "segments": [{"type": "thinking", "text": "hm"}, {"type": "answer", "text": "Hello! How can I help you today?"}],
            "finishreason": "stop",
        },
    }],
}


def test_read_image_task():
    result = read_task(IMAGE_TASK)
    assert [f.kind for f in result.files] == ["image", "image"]
    assert result.urls == ["https://cdn1.wiro.ai/a/0.png", "https://cdn1.wiro.ai/a/1.png"]
    assert result.cost == 0.006
    assert result.summary() == "2 images"
    assert not result.has_llm_output


def test_read_llm_task():
    result = read_task(LLM_TASK)
    assert result.files == [] and result.has_llm_output
    assert result.llm_text() == "Hello! How can I help you today?"
    assert result.summary() == "text"


def test_llm_text_fallbacks():
    task = {**LLM_TASK, "debugoutput": ""}
    assert read_task(task).llm_text() == "Hello! How can I help you today?"
    task["outputs"] = [{"contenttype": "raw", "content": {"segments": [{"type": "answer", "text": "Hi"}, {"type": "answer", "text": " there"}]}}]
    assert read_task(task).llm_text() == "Hi there"


def test_result_text_rules(monkeypatch):
    # Image models: their debug output is not the text output.
    assert runner.result_text(read_task(IMAGE_TASK)) == ""
    # LLM models: debugoutput.
    assert runner.result_text(read_task(LLM_TASK)).startswith("Hello!")
    # Text files are downloaded.
    monkeypatch.setattr(media, "download", lambda url: b"transcript text\n")
    task = {"pexit": "0", "outputs": [{"contenttype": "text/plain", "url": "https://cdn1.wiro.ai/t.txt"}]}
    assert runner.result_text(read_task(task)) == "transcript text"
    # No files at all: the text the model printed.
    assert runner.result_text(read_task({"pexit": "0", "debugoutput": " spoken words ", "outputs": []})) == "spoken words"


def test_outputs_without_url_are_ignored():
    result = read_task({"outputs": [{"contenttype": "image/png", "name": "0.png"}, "junk", None]})
    assert result.files == [] and result.summary() == "no outputs"


def test_missing_outputs_return_none(monkeypatch):
    result = read_task(LLM_TASK)
    assert runner.result_images(result) is None
    assert runner.result_video(result) is None
    assert runner.result_audio(result) is None
