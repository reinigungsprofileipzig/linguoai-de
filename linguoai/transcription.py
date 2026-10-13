"""Faster-Whisper-Adapter mit Chunk-Metadaten, Überlappung und hartem Fehlerstatus."""

from __future__ import annotations

import gc
import math
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .domain import AudioChunk, Segment, TranscriptionResult
from .errors import CancelledError, DependencyError, TranscriptionError
from .runtime import check_cancelled
from .text import clean_text


@dataclass(frozen=True, slots=True)
class _ChunkTranscription:
    chunk: AudioChunk
    segments: tuple[Segment, ...]
    language: str | None
    language_probability: float | None


class FasterWhisperTranscriber:
    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Callable[[str], None] | None = None,
        model: Any | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or (lambda _message: None)
        self._model = model
        self._model_lock = threading.Lock()

    def preflight(self) -> None:
        if self._model is not None:
            return
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise DependencyError(
                "faster-whisper fehlt. Installiere das Projekt mit 'pip install -e .'."
            ) from exc

    def close(self) -> None:
        """Gibt ein geladenes Whisper-Modell nach dem Job wieder frei."""
        with self._model_lock:
            self._model = None
        gc.collect()

    def transcribe(
        self,
        chunks: Sequence[AudioChunk],
        *,
        source_language: str | None,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> TranscriptionResult:
        if not chunks:
            raise TranscriptionError("Keine Audio-Chunks zum Transkribieren vorhanden.")
        check_cancelled(cancel_event)
        self._ensure_model()
        check_cancelled(cancel_event)
        total = len(chunks)

        if source_language:
            language = self._whisper_language(source_language)
            results = self._parallel(
                chunks,
                language=language,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
            probability = None
        else:
            # Sprache nur einmal anhand des ersten sprechenden Chunks ermitteln.
            results: list[_ChunkTranscription] = []
            language = None
            probability = None
            next_index = 0
            for next_index, chunk in enumerate(chunks, start=1):
                result = self._transcribe_with_retry(chunk, None, cancel_event)
                results.append(result)
                if on_progress:
                    on_progress(next_index, total)
                if result.segments and result.language:
                    language = result.language
                    probability = result.language_probability
                    break
            if language is None:
                raise TranscriptionError("Whisper erkannte in der Audiospur keine Sprache.")
            remaining = chunks[next_index:]
            if remaining:
                already_done = next_index

                def remaining_progress(done: int, _count: int) -> None:
                    if on_progress:
                        on_progress(already_done + done, total)

                results.extend(
                    self._parallel(
                        remaining,
                        language=language,
                        cancel_event=cancel_event,
                        on_progress=remaining_progress,
                    )
                )

        ordered = sorted(results, key=lambda item: item.chunk.index)
        segments = tuple(segment for result in ordered for segment in result.segments)
        if not segments:
            raise TranscriptionError("Whisper lieferte keinen verwertbaren Text.")
        return TranscriptionResult(
            segments=segments,
            language=language,
            language_probability=probability,
        )

    def _parallel(
        self,
        chunks: Sequence[AudioChunk],
        *,
        language: str,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None,
    ) -> list[_ChunkTranscription]:
        results: list[_ChunkTranscription] = []
        failures: list[tuple[AudioChunk, BaseException]] = []
        completed = 0
        with ThreadPoolExecutor(
            max_workers=min(self.config.transcription_workers, len(chunks)),
            thread_name_prefix="whisper",
        ) as executor:
            futures = {
                executor.submit(self._transcribe_one, chunk, language, cancel_event): chunk
                for chunk in chunks
            }
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    results.append(future.result())
                except CancelledError:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as exc:
                    failures.append((chunk, exc))
                completed += 1
                if on_progress:
                    on_progress(completed, len(chunks))

        # Ein isolierter Retry fängt vorübergehende OOM-/Decoder-Probleme ab,
        # ohne Lücken zu verstecken.
        final_failures: list[tuple[AudioChunk, BaseException]] = []
        for chunk, original_error in failures:
            check_cancelled(cancel_event)
            self.logger(
                f"Chunk {chunk.index + 1} fehlgeschlagen; einmaliger serieller Retry: "
                f"{original_error}"
            )
            try:
                results.append(self._transcribe_one(chunk, language, cancel_event))
            except CancelledError:
                raise
            except Exception as retry_error:
                final_failures.append((chunk, retry_error))

        if final_failures:
            detail = "; ".join(
                f"Chunk {chunk.index + 1}: {error}" for chunk, error in final_failures
            )
            raise TranscriptionError(f"Transkription ist unvollständig: {detail}")
        return results

    def _transcribe_with_retry(
        self,
        chunk: AudioChunk,
        language: str | None,
        cancel_event: threading.Event,
    ) -> _ChunkTranscription:
        try:
            return self._transcribe_one(chunk, language, cancel_event)
        except CancelledError:
            raise
        except Exception as first_error:
            self.logger(f"Chunk {chunk.index + 1} wird erneut versucht: {first_error}")
            try:
                return self._transcribe_one(chunk, language, cancel_event)
            except CancelledError:
                raise
            except Exception as second_error:
                raise TranscriptionError(
                    f"Chunk {chunk.index + 1} konnte nicht transkribiert werden: {second_error}"
                ) from second_error

    def _transcribe_one(
        self,
        chunk: AudioChunk,
        language: str | None,
        cancel_event: threading.Event,
    ) -> _ChunkTranscription:
        check_cancelled(cancel_event)
        try:
            generated_segments, info = self._model.transcribe(
                str(chunk.path),
                language=language,
                beam_size=5,
                best_of=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                word_timestamps=False,
                temperature=(0.0, 0.2, 0.4, 0.6),
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                no_speech_threshold=0.6,
                condition_on_previous_text=False,
            )
            segments: list[Segment] = []
            final_chunk = math.isclose(chunk.keep_end, chunk.media_end, abs_tol=1e-3)
            for generated in generated_segments:
                check_cancelled(cancel_event)
                text = clean_text(generated.text or "")
                if not text:
                    continue
                start = max(chunk.media_start, chunk.media_start + float(generated.start))
                end = min(chunk.media_end, chunk.media_start + float(generated.end))
                if end <= start:
                    continue
                midpoint = (start + end) / 2
                if midpoint < chunk.keep_start:
                    continue
                if midpoint >= chunk.keep_end and not final_chunk:
                    continue
                segments.append(Segment(start=start, end=end, text=text))
            return _ChunkTranscription(
                chunk=chunk,
                segments=tuple(segments),
                language=getattr(info, "language", language),
                language_probability=getattr(info, "language_probability", None),
            )
        except CancelledError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Whisper-Fehler in Chunk {chunk.index + 1}: {exc}") from exc

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise DependencyError(
                    "faster-whisper fehlt. Installiere das Projekt mit 'pip install -e .'."
                ) from exc
            device = self.config.device
            if device == "auto":
                try:
                    import ctranslate2

                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except (ImportError, OSError, RuntimeError):
                    device = "cpu"
            compute_type = self.config.compute_type
            if compute_type == "default":
                compute_type = "float16" if device == "cuda" else "int8"
            self.logger(f"Lade Whisper-Modell {self.config.whisper_model!r} auf {device} …")
            try:
                cache_root = Path(
                    os.environ.get("LINGUOAI_MODEL_CACHE")
                    or os.environ.get("HF_HOME")
                    or Path.home() / ".cache" / "linguoai"
                )
                self._model = WhisperModel(
                    self.config.whisper_model,
                    device=device,
                    compute_type=compute_type,
                    num_workers=self.config.transcription_workers,
                    download_root=str(cache_root / "whisper"),
                )
            except Exception as exc:
                if self.config.device == "auto" and device == "cuda":
                    self.logger(
                        "Whisper-CUDA konnte nicht initialisiert werden; versuche CPU/int8."
                    )
                    try:
                        self._model = WhisperModel(
                            self.config.whisper_model,
                            device="cpu",
                            compute_type="int8",
                            num_workers=self.config.transcription_workers,
                            download_root=str(cache_root / "whisper"),
                        )
                        return
                    except Exception as cpu_error:
                        raise TranscriptionError(
                            "Whisper-Modell konnte weder auf CUDA noch auf CPU geladen werden: "
                            f"CUDA={exc}; CPU={cpu_error}"
                        ) from cpu_error
                raise TranscriptionError(
                    f"Whisper-Modell konnte nicht geladen werden: {exc}"
                ) from exc

    @staticmethod
    def _whisper_language(language: str) -> str:
        return language.split("-", maxsplit=1)[0].lower()
