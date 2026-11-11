import io

import pytest
import torch
from PIL import Image

from wiro_comfyui import media
from wiro_comfyui.client import WiroError


def test_image_round_trip():
    images = torch.rand(2, 5, 7, 3)
    blobs = media.image_batch_to_png(images)
    assert len(blobs) == 2 and all(b.startswith(b"\x89PNG") for b in blobs)
    decoded = media.decode_images(blobs)
    assert decoded.shape == (2, 5, 7, 3)
    assert torch.allclose(decoded, images, atol=1 / 255)


def test_image_alpha_and_single_frame():
    rgba = torch.ones(5, 4, 4)  # no batch dimension, 4 channels
    blob = media.image_batch_to_png(rgba)[0]
    assert Image.open(io.BytesIO(blob)).mode == "RGBA"
    assert media.decode_images([blob]).shape == (1, 5, 4, 3)


def test_decode_images_resizes_to_the_first():
    def png(size):
        buffer = io.BytesIO()
        Image.new("RGB", size, (0, 0, 255)).save(buffer, format="PNG")
        return buffer.getvalue()

    batch = media.decode_images([png((8, 6)), png((16, 12))])
    assert batch.shape == (2, 6, 8, 3)
    with pytest.raises(WiroError):
        media.decode_images([])


@pytest.mark.parametrize("channels", [1, 2])
def test_audio_round_trip(channels):
    sample_rate = 16000
    t = torch.linspace(0, 1, sample_rate)
    wave = (0.5 * torch.sin(2 * torch.pi * 440 * t)).repeat(channels, 1).unsqueeze(0)
    blob = media.audio_to_wav({"waveform": wave, "sample_rate": sample_rate})
    assert blob[:4] == b"RIFF"
    audio = media.decode_audio(blob)
    assert audio["sample_rate"] == sample_rate
    assert audio["waveform"].shape == (1, channels, sample_rate)
    assert torch.allclose(audio["waveform"], wave, atol=1e-3)


def test_audio_input_must_be_audio():
    with pytest.raises(WiroError):
        media.audio_to_wav({"nope": 1})


@pytest.mark.parametrize(
    "ctype, name, kind",
    [
        ("image/png", "0.png", "image"),
        ("image/webp", "", "image"),
        ("video/mp4", "", "video"),
        ("audio/mpeg", "", "audio"),
        ("raw", "", "raw"),
        ("text/plain", "", "text"),
        ("application/json", "", "text"),
        ("", "https://cdn1.wiro.ai/x/out.MP4", "video"),
        ("application/octet-stream", "speech.mp3", "audio"),
        ("", "result.srt", "text"),
        ("application/zip", "model.zip", "other"),
    ],
)
def test_media_kind(ctype, name, kind):
    assert media.media_kind(ctype, name) == kind


def test_download_rejects_non_urls():
    with pytest.raises(WiroError, match="not a URL"):
        media.download("E")
    with pytest.raises(WiroError, match="not a URL"):
        media.download("file:///etc/passwd")
