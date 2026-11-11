import io
import os
import sys

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for path in (ROOT, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)

from helpers import FakeSession  # noqa: E402


@pytest.fixture
def fake_session():
    return FakeSession


@pytest.fixture
def png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("WIRO_API_KEY", raising=False)
    monkeypatch.delenv("WIRO_API_SECRET", raising=False)


@pytest.fixture(autouse=True)
def fresh_model_cache():
    from wiro_comfyui import runner

    runner._info_cache.clear()
    yield
    runner._info_cache.clear()
