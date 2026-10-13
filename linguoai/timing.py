"""Adaptive Zeitplanung für natürlichere, cue-nahe Sprachsynchronisation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .domain import Segment


@dataclass(frozen=True, slots=True)
class TimingAdjustment:
    segments: tuple[Segment, ...]
    changed: bool = False
    borrowed_seconds: float = 0.0
    shifted_boundaries: int = 0


@dataclass(frozen=True, slots=True)
class MergeAdjustment:
    segments: tuple[Segment, ...]
    changed: bool = False
    merge_count: int = 0


def required_speeds(
    segments: Sequence[Segment],
    audio_durations: Sequence[float],
) -> list[float]:
    _validate_parallel(segments, audio_durations)
    return [
        audio / segment.duration for segment, audio in zip(segments, audio_durations, strict=True)
    ]


def expand_into_free_gaps(
    segments: Sequence[Segment],
    audio_durations: Sequence[float],
    *,
    media_duration: float,
) -> TimingAdjustment:
    """Leiht einem Segment ungenutzte Stille links/rechts, ohne Reihenfolge zu verändern."""
    _validate_parallel(segments, audio_durations)
    if not segments:
        return TimingAdjustment(())
    if not math.isfinite(media_duration) or media_duration <= 0:
        raise ValueError("Mediendauer muss positiv und endlich sein.")

    left_extra = [0.0] * len(segments)
    right_extra = [0.0] * len(segments)
    remaining = [
        max(0.0, audio - segment.duration)
        for segment, audio in zip(segments, audio_durations, strict=True)
    ]

    prefix_gap = max(0.0, segments[0].start)
    take = min(prefix_gap, remaining[0])
    left_extra[0] += take
    remaining[0] -= take

    suffix_gap = max(0.0, media_duration - segments[-1].end)
    take = min(suffix_gap, remaining[-1])
    right_extra[-1] += take
    remaining[-1] -= take

    for index in range(len(segments) - 1):
        gap = max(0.0, segments[index + 1].start - segments[index].end)
        if gap <= 1e-9:
            continue
        left_need = remaining[index]
        right_need = remaining[index + 1]
        total_need = left_need + right_need
        if total_need <= 1e-9:
            continue
        if total_need <= gap:
            give_left = left_need
            give_right = right_need
        else:
            give_left = gap * left_need / total_need
            give_right = gap - give_left
        right_extra[index] += give_left
        left_extra[index + 1] += give_right
        remaining[index] = max(0.0, left_need - give_left)
        remaining[index + 1] = max(0.0, right_need - give_right)

    adjusted: list[Segment] = []
    borrowed = 0.0
    for index, segment in enumerate(segments):
        start = max(0.0, segment.start - left_extra[index])
        end = min(media_duration, segment.end + right_extra[index])
        borrowed += (segment.start - start) + (end - segment.end)
        adjusted.append(
            Segment(
                start=start,
                end=end,
                text=segment.text,
                original_text=segment.original_text,
            )
        )
    return TimingAdjustment(
        tuple(adjusted),
        changed=borrowed > 1e-6,
        borrowed_seconds=borrowed,
    )


def rebalance_neighboring_windows(
    segments: Sequence[Segment],
    audio_durations: Sequence[float],
    *,
    preferred_speed: float,
    maximum_gap: float = 0.45,
    maximum_group_size: int = 4,
    maximum_boundary_shift: float = 1.25,
    minimum_slot: float = 0.18,
) -> TimingAdjustment:
    """Verschiebt Grenzen kleiner Nachbargruppen zugunsten zu dichter Cues."""
    _validate_parallel(segments, audio_durations)
    if not segments:
        return TimingAdjustment(())
    if preferred_speed < 1.0:
        raise ValueError("Bevorzugtes Tempo muss mindestens 1,0 sein.")

    result = list(segments)
    shifted = 0
    index = 0
    while index < len(result) - 1:
        best_end: int | None = None
        for end in range(index + 1, min(len(result), index + maximum_group_size)):
            previous = result[end - 1]
            current = result[end]
            if current.start - previous.end > maximum_gap:
                break
            group = result[index : end + 1]
            durations = audio_durations[index : end + 1]
            total_span = group[-1].end - group[0].start
            if total_span <= minimum_slot * len(group):
                continue
            speeds = [audio / cue.duration for cue, audio in zip(group, durations, strict=True)]
            group_speed = sum(durations) / total_span
            if max(speeds) > preferred_speed and group_speed <= preferred_speed:
                best_end = end
        if best_end is None:
            index += 1
            continue

        group = result[index : best_end + 1]
        durations = list(audio_durations[index : best_end + 1])
        group_start = group[0].start
        group_end = group[-1].end
        usable = group_end - group_start
        total_audio = sum(durations)
        if total_audio <= 1e-9:
            index = best_end + 1
            continue

        boundaries = [group_start]
        cumulative_audio = 0.0
        for local in range(len(group) - 1):
            cumulative_audio += durations[local]
            desired = group_start + usable * cumulative_audio / total_audio
            original_boundary = (group[local].end + group[local + 1].start) / 2.0
            lower = max(
                boundaries[-1] + minimum_slot,
                original_boundary - maximum_boundary_shift,
            )
            remaining_count = len(group) - local - 1
            upper = min(
                group_end - minimum_slot * remaining_count,
                original_boundary + maximum_boundary_shift,
            )
            boundary = min(max(desired, lower), upper)
            boundaries.append(boundary)
        boundaries.append(group_end)

        for local, cue in enumerate(group):
            start = boundaries[local]
            end = boundaries[local + 1]
            if abs(start - cue.start) > 1e-6 or abs(end - cue.end) > 1e-6:
                shifted += 1
            result[index + local] = Segment(
                start=start,
                end=end,
                text=cue.text,
                original_text=cue.original_text,
            )
        index = best_end + 1

    return TimingAdjustment(
        tuple(result),
        changed=shifted > 0,
        shifted_boundaries=shifted,
    )


def merge_overfull_segments(
    segments: Sequence[Segment],
    audio_durations: Sequence[float],
    *,
    preferred_speed: float,
    maximum_gap: float = 1.25,
    maximum_group_duration: float = 18.0,
    maximum_characters: int = 420,
) -> MergeAdjustment:
    """Bündelt problematische Nachbar-Cues, damit TTS-Pausen nicht mehrfach anfallen."""
    _validate_parallel(segments, audio_durations)
    working = list(segments)
    estimates = list(audio_durations)
    merge_count = 0

    while len(working) > 1:
        speeds = required_speeds(working, estimates)
        overfull = [index for index, speed in enumerate(speeds) if speed > preferred_speed]
        if not overfull:
            break
        candidates: list[tuple[float, int, int]] = []
        for problem in sorted(overfull, key=speeds.__getitem__, reverse=True):
            for left, right in ((problem - 1, problem), (problem, problem + 1)):
                if left < 0 or right >= len(working):
                    continue
                first = working[left]
                second = working[right]
                gap = max(0.0, second.start - first.end)
                span = second.end - first.start
                combined_text = f"{first.text} {second.text}".strip()
                if (
                    gap > maximum_gap
                    or span > maximum_group_duration
                    or len(combined_text) > maximum_characters
                ):
                    continue
                # Eine gemeinsame Synthese spart typischerweise mindestens eine Cue-Pause.
                estimated_audio = max(0.05, (estimates[left] + estimates[right]) * 0.94)
                score = estimated_audio / span
                candidates.append((score, left, right))
            if candidates:
                break
        if not candidates:
            break

        _score, left, right = min(candidates, key=lambda item: item[0])
        first = working[left]
        second = working[right]
        original_parts = [part for part in (first.original_text, second.original_text) if part]
        merged = Segment(
            start=first.start,
            end=second.end,
            text=f"{first.text} {second.text}".strip(),
            original_text=" ".join(original_parts) or None,
        )
        working[left : right + 1] = [merged]
        estimates[left : right + 1] = [max(0.05, (estimates[left] + estimates[right]) * 0.94)]
        merge_count += 1

    return MergeAdjustment(
        tuple(working),
        changed=merge_count > 0,
        merge_count=merge_count,
    )


def _validate_parallel(segments: Sequence[Segment], audio_durations: Sequence[float]) -> None:
    if len(segments) != len(audio_durations):
        raise ValueError("Segment- und Audiodauer-Anzahl stimmen nicht überein.")
    for duration in audio_durations:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Audiodauern müssen positiv und endlich sein.")
