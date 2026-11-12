"""Conversions between ComfyUI types and the files Wiro takes and returns.

ComfyUI types:
- IMAGE: float tensor [batch, height, width, channels] in 0..1
- AUDIO: {"waveform": float tensor [batch, channels, samples], "sample_rate": int}
- VIDEO: a comfy_api VideoInput (VideoFromFile for downloaded results)
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import uuid
import wave
from typing import Any, Optional
from urllib.parse import urlparse

import numpy as np
import requests
import torch
from PIL import Image, ImageOps

from .client import USER_AGENT, WiroError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"}
TEXT_EXTENSIONS = {".txt", ".json", ".srt", ".vtt", ".md", ".csv"}
MODEL_EXTENSIONS = {".glb", ".gltf", ".obj", ".fbx", ".stl", ".ply", ".usdz"}

_DOWNLOAD_TIMEOUT = (15, 600)
_MAX_TEXT_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------- inputs


def image_batch_to_png(images: torch.Tensor) -> list[bytes]:
    """One PNG per image in an IMAGE batch."""
    if images is None:
        return []
    if images.dim() == 3:
        images = images.unsqueeze(0)
    array = (images.detach().cpu().float().clamp(0, 1) * 255.0).round().to(torch.uint8).numpy()
    result = []
    for frame in array:
        channels = frame.shape[-1]
        if channels == 1:
            pil = Image.fromarray(frame[..., 0], "L")
        elif channels == 4:
            pil = Image.fromarray(frame, "RGBA")
        else:
            pil = Image.fromarray(frame[..., :3], "RGB")
        buffer = io.BytesIO()
        pil.save(buffer, format="PNG")
        result.append(buffer.getvalue())
    return result


def audio_to_wav(audio: dict) -> bytes:
    """16-bit PCM WAV of the first clip in an AUDIO value."""
    try:
        waveform = audio["waveform"]
        sample_rate = int(audio["sample_rate"])
    except (KeyError, TypeError):
        raise WiroError("The audio input is not a ComfyUI AUDIO value.") from None
    if waveform.dim() == 3:
        waveform = waveform[0]
    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    pcm = (waveform.detach().cpu().float().clamp(-1, 1) * 32767.0).round().to(torch.int16)
    frames = np.ascontiguousarray(pcm.numpy().T)  # [samples, channels]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(frames.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames.tobytes())
    return buffer.getvalue()


def video_to_mp4(video: Any) -> bytes:
    """MP4 bytes of a VIDEO input (trims and edits applied by ComfyUI)."""
    try:
        from comfy_api.latest import Types

        container, codec = Types.VideoContainer.MP4, Types.VideoCodec.H264
    except ImportError:
        try:
            from comfy_api.util import VideoCodec, VideoContainer

            container, codec = VideoContainer.MP4, VideoCodec.H264
        except ImportError:
            container = codec = None
    if not hasattr(video, "save_to"):
        raise WiroError("The video input is not a ComfyUI VIDEO value.")
    path = os.path.join(_temp_dir(), f"wiro-input-{uuid.uuid4().hex}.mp4")
    try:
        if container is not None:
            video.save_to(path, format=container, codec=codec)
        else:
            video.save_to(path)
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- outputs


def media_kind(content_type: str, name: str) -> str:
    """image, video, audio, text, 3d, raw or other."""
    ctype = (content_type or "").lower().split(";")[0].strip()
    ext = os.path.splitext(urlparse(name or "").path)[1].lower()
    if ctype == "raw":
        return "raw"
    if ctype.startswith("model/") or ext in MODEL_EXTENSIONS:
        return "3d"
    for prefix in ("image", "video", "audio"):
        if ctype.startswith(prefix + "/"):
            return prefix
    if ctype.startswith("text/") or ctype == "application/json":
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "other"


def download(url: str, *, attempts: int = 3) -> bytes:
    """Download a result file. Wiro output URLs are public CDN links."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise WiroError(f"Wiro returned an output that is not a URL: {url!r}")
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=_DOWNLOAD_TIMEOUT, stream=True
            ) as response:
                response.raise_for_status()
                return response.content
        except requests.RequestException as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise WiroError(f"Could not download {url}: {last}")


def decode_images(blobs: list[bytes]) -> torch.Tensor:
    """IMAGE batch from encoded images. Images of other sizes are resized to the first one."""
    pictures = []
    for blob in blobs:
        picture = Image.open(io.BytesIO(blob))
        picture = ImageOps.exif_transpose(picture)
        pictures.append(picture.convert("RGB"))
    if not pictures:
        raise WiroError("No images to decode.")
    size = pictures[0].size
    tensors = []
    for picture in pictures:
        if picture.size != size:
            picture = picture.resize(size, Image.LANCZOS)
        array = np.asarray(picture).astype(np.float32) / 255.0
        tensors.append(torch.from_numpy(array))
    return torch.stack(tensors, dim=0)


def decode_images_with_alpha(blobs: list) -> tuple:
    """(IMAGE batch, MASK batch) from images with transparency. The mask is
    the alpha channel: 1 where the subject is, 0 where the background was."""
    pictures = []
    for blob in blobs:
        picture = ImageOps.exif_transpose(Image.open(io.BytesIO(blob)))
        pictures.append(picture.convert("RGBA"))
    if not pictures:
        raise WiroError("No images to decode.")
    size = pictures[0].size
    rgb, alpha = [], []
    for picture in pictures:
        if picture.size != size:
            picture = picture.resize(size, Image.LANCZOS)
        array = np.asarray(picture).astype(np.float32) / 255.0
        rgb.append(torch.from_numpy(array[..., :3].copy()))
        alpha.append(torch.from_numpy(array[..., 3].copy()))
    return torch.stack(rgb, dim=0), torch.stack(alpha, dim=0)


def file3d_from_bytes(blob: bytes, name: str = "model.glb") -> Any:
    """A ComfyUI 3D file value (File3D) saved in ComfyUI's temp folder."""
    ext = os.path.splitext(urlparse(name).path)[1].lower() or ".glb"
    if ext not in MODEL_EXTENSIONS:
        ext = ".glb"
    path = os.path.join(_temp_dir(), f"wiro-{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as handle:
        handle.write(blob)
    try:
        from comfy_api.latest import Types

        return Types.File3D(path, ext.lstrip("."))
    except (ImportError, AttributeError):
        raise WiroError("This ComfyUI version has no 3D file type. Update ComfyUI, or use the urls output.") from None


def decode_audio(blob: bytes) -> dict:
    """AUDIO value from an encoded audio file (mp3, wav, flac, ...)."""
    import av  # ComfyUI depends on PyAV

    chunks = []
    with av.open(io.BytesIO(blob)) as container:
        if not container.streams.audio:
            raise WiroError("The audio file has no audio stream.")
        stream = container.streams.audio[0]
        sample_rate = int(stream.codec_context.sample_rate or stream.rate or 44100)
        channels = int(stream.codec_context.channels or 1)
        for frame in container.decode(stream):
            array = frame.to_ndarray()
            if array.ndim == 1:
                array = array.reshape(1, -1)
            if array.shape[0] != channels:
                # Packed formats come as [1, samples * channels].
                array = array.reshape(-1, channels).T
            if np.issubdtype(array.dtype, np.integer):
                info = np.iinfo(array.dtype)
                array = array.astype(np.float32) / float(max(abs(info.min), info.max))
            chunks.append(torch.from_numpy(np.ascontiguousarray(array, dtype=np.float32)))
    if not chunks:
        raise WiroError("The audio file has no samples.")
    waveform = torch.cat(chunks, dim=1)
    return {"waveform": waveform.unsqueeze(0).contiguous(), "sample_rate": sample_rate}


def video_from_bytes(blob: bytes, name: str = "video.mp4") -> Any:
    """VIDEO value backed by a file in ComfyUI's temp folder."""
    ext = os.path.splitext(urlparse(name).path)[1].lower() or ".mp4"
    if ext not in VIDEO_EXTENSIONS:
        ext = ".mp4"
    path = os.path.join(_temp_dir(), f"wiro-{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as handle:
        handle.write(blob)
    try:
        from comfy_api.latest import InputImpl

        return InputImpl.VideoFromFile(path)
    except ImportError:
        try:
            from comfy_api.input_impl import VideoFromFile

            return VideoFromFile(path)
        except ImportError:
            raise WiroError(
                "This ComfyUI version has no VIDEO type. Update ComfyUI, or use the urls output."
            ) from None


def decode_text(blob: bytes) -> str:
    return blob[:_MAX_TEXT_BYTES].decode("utf-8", errors="replace").strip()


def _temp_dir() -> str:
    try:
        import folder_paths

        path = folder_paths.get_temp_directory()
    except Exception:
        path = os.path.join(tempfile.gettempdir(), "comfyui-wiro")
    os.makedirs(path, exist_ok=True)
    return path
