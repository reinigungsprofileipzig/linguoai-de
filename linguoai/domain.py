"""Kleine typisierte Datenobjekte statt lose strukturierter Dictionaries."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str
    original_text: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("Segmentzeiten müssen endlich sein.")
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"Ungültiges Segmentintervall: {self.start}-{self.end}")
        if not self.text.strip():
            raise ValueError("Segmenttext darf nicht leer sein.")

    @property
    def duration(self) -> float:
        return self.end - self.start

    def with_text(self, text: str, *, remember_original: bool = False) -> Segment:
        original = self.original_text
        if remember_original and original is None:
            original = self.text
        return replace(self, text=text.strip(), original_text=original)


@dataclass(frozen=True, slots=True)
class AudioChunk:
    index: int
    path: Path
    media_start: float
    media_end: float
    keep_start: float
    keep_end: float

    @property
    def duration(self) -> float:
        return self.media_end - self.media_start


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    segments: tuple[Segment, ...]
    language: str | None
    language_probability: float | None = None


@dataclass(frozen=True, slots=True)
class JobResult:
    input_path: Path
    output_path: Path
    original_subtitles: Path
    translated_subtitles: Path
    source_language: str | None
    segment_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobFailure:
    input_path: Path
    error: str


@dataclass(frozen=True, slots=True)
class BatchResult:
    completed: tuple[JobResult, ...] = ()
    failed: tuple[JobFailure, ...] = ()

    @property
    def succeeded(self) -> bool:
        return bool(self.completed) and not self.failed


class Stage(str, Enum):
    PREPARING = "preparing"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    OPTIMIZING = "optimizing"
    TRANSLATING = "translating"
    SYNTHESIZING = "synthesizing"
    SYNCHRONIZING = "synchronizing"
    MUXING = "muxing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    percent: float
    stage: Stage
    message: str
    input_path: Path | None = None
    file_index: int = 1
    file_count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "percent", max(0.0, min(100.0, self.percent)))


@dataclass(slots=True)
class MutableWarnings:
    items: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        if message not in self.items:
            self.items.append(message)
