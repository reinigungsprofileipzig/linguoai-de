"""Reine Text- und Segmentfunktionen, unabhängig von Cloud-Diensten."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from .domain import Segment
from .errors import TranslationError

_WHITESPACE = re.compile(r"\s+")
_ENTITY = re.compile(
    r"https?://[^\s<>\[\]{}\"']+(?<![.,!?;:])"
    r"|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|(?<!\w)(?:[€$£¥]\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*"
    r"(?:kWh|MHz|GHz|km|cm|mm|kg|mg|ml|€|\$|£|¥|%|°C|°F))(?!\w)",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True, slots=True)
class ProtectedText:
    text: str
    entities: tuple[tuple[str, str], ...]


def protect_entities(text: str) -> ProtectedText:
    entities: list[tuple[str, str]] = []
    nonce = uuid.uuid4().hex.upper()
    prefix = f"__LINGUO_{nonce}_"
    while prefix in text:
        nonce = uuid.uuid4().hex.upper()
        prefix = f"__LINGUO_{nonce}_"

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        trailing = ""
        if value.startswith(("http://", "https://")):
            while value.endswith(")") and value.count(")") > value.count("("):
                value = value[:-1]
                trailing = ")" + trailing
        token = f"{prefix}{len(entities):04d}__"
        entities.append((token, value))
        return token + trailing

    return ProtectedText(_ENTITY.sub(replace, text), tuple(entities))


def restore_entities(text: str, entities: tuple[tuple[str, str], ...]) -> str:
    restored = text
    invalid_count = 0
    for token, value in entities:
        if restored.count(token) != 1:
            invalid_count += 1
            continue
        restored = restored.replace(token, value, 1)
    if invalid_count:
        raise TranslationError(
            f"{invalid_count} geschützte Inhalte fehlen oder sind doppelt in der Übersetzung."
        )
    return clean_text(restored)


def _normalized_for_comparison(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def deduplicate_overlapping_segments(segments: Iterable[Segment]) -> list[Segment]:
    """Entfernt Duplikate aus überlappenden Whisper-Chunks, nicht echte Wiederholungen."""
    result: list[Segment] = []
    for current in sorted(segments, key=lambda item: (item.start, item.end)):
        if not result:
            result.append(current)
            continue
        previous = result[-1]
        overlap = min(previous.end, current.end) - max(previous.start, current.start)
        if overlap <= 0:
            result.append(current)
            continue
        left = _normalized_for_comparison(previous.text)
        right = _normalized_for_comparison(current.text)
        similarity = SequenceMatcher(None, left, right).ratio() if left and right else 0.0
        if similarity >= 0.9:
            if len(current.text) > len(previous.text):
                result[-1] = current
            continue
        result.append(current)
    return result


def sanitize_segments(
    segments: Iterable[Segment],
    *,
    media_duration: float,
    minimum_duration: float = 0.08,
) -> list[Segment]:
    """Sortiert, begrenzt und entkoppelt überlappende Zeitintervalle."""
    clean: list[Segment] = []
    previous_end = 0.0
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        start = max(0.0, previous_end, segment.start)
        end = min(media_duration, segment.end)
        text = clean_text(segment.text)
        if not text or end - start < minimum_duration:
            continue
        clean.append(
            Segment(
                start=start,
                end=end,
                text=text,
                original_text=segment.original_text,
            )
        )
        previous_end = end
    return clean


def coalesce_segments(
    segments: Iterable[Segment],
    *,
    maximum_duration: float = 12.0,
    maximum_characters: int = 220,
    maximum_gap: float = 0.35,
) -> list[Segment]:
    """Verbindet sehr kleine ASR-Cues zu TTS-tauglichen, stabilen Zeitfenstern."""
    source = list(segments)
    if not source:
        return []
    merged: list[Segment] = [source[0]]
    for current in source[1:]:
        previous = merged[-1]
        combined_text = f"{previous.text} {current.text}".strip()
        can_merge = (
            current.start - previous.end <= maximum_gap
            and current.end - previous.start <= maximum_duration
            and len(combined_text) <= maximum_characters
        )
        if can_merge:
            merged[-1] = Segment(previous.start, current.end, combined_text)
        else:
            merged.append(current)
    return merged


def batches_by_size(
    texts: list[str], *, maximum_items: int = 20, maximum_characters: int = 6_000
) -> list[tuple[int, int]]:
    """Liefert halb-offene Indexbereiche, die beide Limits garantiert einhalten."""
    batches: list[tuple[int, int]] = []
    start = 0
    size = 0
    for index, text in enumerate(texts):
        if len(text) > maximum_characters:
            raise TranslationError(f"Ein einzelnes Segment ist mit {len(text)} Zeichen zu lang.")
        if index > start and (
            index - start >= maximum_items or size + len(text) > maximum_characters
        ):
            batches.append((start, index))
            start = index
            size = 0
        size += len(text)
    if start < len(texts):
        batches.append((start, len(texts)))
    return batches
