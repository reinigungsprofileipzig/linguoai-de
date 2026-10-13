"""SRT-Ausgabe mit korrekter Rundung und atomarem Schreiben."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from .domain import Segment


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1_000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def render_srt(segments: list[Segment] | tuple[Segment, ...]) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = segment.text.strip().replace("\r\n", "\n").replace("\r", "\n")
        blocks.append(
            f"{index}\n"
            f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def write_srt_atomic(path: Path, segments: list[Segment] | tuple[Segment, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        temporary.write_text(render_srt(segments), encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
