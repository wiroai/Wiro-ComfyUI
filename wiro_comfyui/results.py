"""Reads a finished Task/Detail record: which outputs are images, videos,
audio or text, and the model's text answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .media import media_kind


@dataclass
class OutputFile:
    url: str
    name: str
    content_type: str
    kind: str


@dataclass
class TaskResult:
    task: dict
    files: list = field(default_factory=list)
    has_llm_output: bool = False

    def of_kind(self, kind: str) -> list:
        return [f for f in self.files if f.kind == kind]

    @property
    def urls(self) -> list:
        return [f.url for f in self.files if f.url]

    @property
    def task_id(self) -> str:
        return str(self.task.get("id") or "")

    @property
    def cost(self) -> Optional[float]:
        try:
            return float(self.task.get("totalcost"))
        except (TypeError, ValueError):
            return None

    def llm_text(self) -> str:
        """The answer text of an LLM task.

        debugoutput holds the merged text. If it is empty, fall back to the
        structured content: "raw", then the answer segments.
        """
        text = str(self.task.get("debugoutput") or "").strip()
        if text:
            return text
        for output in self.task.get("outputs") or []:
            if not isinstance(output, dict) or str(output.get("contenttype")).lower() != "raw":
                continue
            content = output.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, dict):
                raw = content.get("raw")
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
                answers = [
                    str(s.get("text") or "")
                    for s in content.get("segments") or []
                    if isinstance(s, dict) and s.get("type") == "answer"
                ]
                if any(answers):
                    return "".join(answers).strip()
        return ""

    def summary(self) -> str:
        counts = {}
        for item in self.files:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        parts = [f"{n} {kind}" + ("s" if n > 1 and kind != "audio" else "") for kind, n in counts.items()]
        if self.has_llm_output:
            parts.append("text")
        return ", ".join(parts) or "no outputs"


def read_task(task: dict) -> TaskResult:
    result = TaskResult(task=task)
    for output in task.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        ctype = str(output.get("contenttype") or "")
        url = str(output.get("url") or "")
        name = str(output.get("name") or url)
        kind = media_kind(ctype, name or url)
        if kind == "raw":
            result.has_llm_output = True
            continue
        if not url:
            continue
        result.files.append(OutputFile(url=url, name=name, content_type=ctype, kind=kind))
    return result
