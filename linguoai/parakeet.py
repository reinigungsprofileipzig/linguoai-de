"""Optionaler lokaler Parakeet-TDT-Adapter für sherpa-onnx auf CPU."""

from __future__ import annotations

import gc
import math
import threading
import wave
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Any

from .config import AppConfig
from .domain import AudioChunk, Segment, TranscriptionResult
from .errors import CancelledError, ConfigurationError, DependencyError, TranscriptionError
from .runtime import check_cancelled
from .text import clean_text


class ParakeetTranscriber:
    """Keine Downloads: Der Nutzer wählt einen kompatiblen ONNX-Export vorab aus.

    Sherpa liefert für Parakeet Token-Anfangszeiten, aber keine Sprachmetadaten.
    Wortenden werden aus dem nächsten Wortanfang (max. 0,5 s Nachlauf) geschätzt.
    Die Quellsprache dient der Übersetzung; sie erzwingt keine Decoder-Sprache.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Callable[[str], None] | None = None,
        recognizer: Any | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or (lambda _message: None)
        self._recognizer = recognizer

    def preflight(self) -> None:
        self.config.validate_parakeet_language(self.config.source_language)
        if self._recognizer is not None:
            return
        self._model_files()
        try:
            import numpy  # noqa: F401
            import sherpa_onnx  # noqa: F401
        except (ImportError, OSError) as exc:
            raise DependencyError(
                "Parakeet benötigt sherpa-onnx und numpy. "
                "Installiere sie mit 'pip install -e \".[parakeet]\"'."
            ) from exc

    def close(self) -> None:
        self._recognizer = None
        gc.collect()

    def _model_files(self) -> dict[str, str]:
        directory = self.config.parakeet_model_directory
        if directory is None or not directory.is_dir():
            raise ConfigurationError("Wähle einen lokalen Parakeet-sherpa-onnx-Modellordner.")
        files: dict[str, str] = {}
        for role in ("encoder", "decoder", "joiner"):
            candidates = (directory / f"{role}.int8.onnx", directory / f"{role}.onnx")
            path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if path is None:
                raise ConfigurationError(
                    f"Im Parakeet-Modellordner fehlt {role}.int8.onnx oder {role}.onnx. "
                    "Nutze einen vollständigen sherpa-onnx-Export, kein .nemo-Modell."
                )
            files[role] = str(path)
        tokens = directory / "tokens.txt"
        if not tokens.is_file():
            raise ConfigurationError(f"Parakeet-Tokenliste fehlt: {tokens}")
        files["tokens"] = str(tokens)
        return files

    def _ensure_model(self) -> None:
        if self._recognizer is not None:
            return
        self.preflight()
        import sherpa_onnx

        self.logger(
            f"Lade Parakeet {self.config.parakeet_profile} auf CPU "
            f"({self.config.parakeet_threads} Threads) …"
        )
        try:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                **self._model_files(),
                model_type="nemo_transducer",
                num_threads=self.config.parakeet_threads,
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                provider="cpu",
            )
        except Exception as exc:
            raise TranscriptionError(
                "Parakeet konnte nicht geladen werden. Prüfe den sherpa-onnx-Export "
                f"und seine externen .onnx.data-Dateien: {exc}"
            ) from exc

    def transcribe(
        self,
        chunks: Sequence[AudioChunk],
        *,
        source_language: str | None,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> TranscriptionResult:
        check_cancelled(cancel_event)
        language = self.config.validate_parakeet_language(source_language)
        if not chunks:
            raise TranscriptionError("Keine Audio-Chunks zum Transkribieren vorhanden.")
        self._ensure_model()
        segments: list[Segment] = []
        # Seriell: Jeder Decoder nutzt bereits die konfigurierten CPU-Threads.
        for done, chunk in enumerate(sorted(chunks, key=lambda item: item.index), start=1):
            check_cancelled(cancel_event)
            segments.extend(self._transcribe_one(chunk, cancel_event))
            if on_progress:
                on_progress(done, len(chunks))
        if not segments:
            raise TranscriptionError("Parakeet lieferte keinen verwertbaren Text.")
        return TranscriptionResult(tuple(segments), language, None)

    def _transcribe_one(
        self, chunk: AudioChunk, cancel_event: threading.Event
    ) -> tuple[Segment, ...]:
        try:
            import numpy as np

            with wave.open(str(chunk.path), "rb") as audio:
                if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (
                    1,
                    16000,
                    2,
                ):
                    raise TranscriptionError("Parakeet erwartet Mono-WAV mit 16 kHz/16 Bit.")
                samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
                samples = samples.astype(np.float32) / 32768.0
            check_cancelled(cancel_event)
            stream = self._recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            # Der native Decoder kann nur zwischen Chunks abgebrochen werden.
            self._recognizer.decode_stream(stream)
            check_cancelled(cancel_event)
            return self._segments(stream.result, chunk)
        except CancelledError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Parakeet-Fehler in Chunk {chunk.index + 1}: {exc}") from exc

    @staticmethod
    def _segments(result: Any, chunk: AudioChunk) -> tuple[Segment, ...]:
        if not clean_text(getattr(result, "text", "") or ""):
            return ()
        tokens = list(getattr(result, "tokens", ()) or ())
        timestamps = list(getattr(result, "timestamps", ()) or ())
        if not tokens or len(tokens) != len(timestamps):
            raise TranscriptionError(
                "Parakeet liefert keine passenden Token-Zeitstempel; "
                "für synchronisierte Untertitel wird ein kompatibler TDT-Export benötigt."
            )
        times = [float(value) for value in timestamps]
        if any(not math.isfinite(value) or value < 0 for value in times) or any(
            right < left for left, right in pairwise(times)
        ):
            raise TranscriptionError("Parakeet liefert ungültige Token-Zeitstempel.")

        words: list[tuple[str, float, float]] = []
        pieces: list[str] = []
        start = last = 0.0
        for token, timestamp in zip(tokens, times, strict=True):
            token = str(token).replace("\u2581", " ")
            if token.startswith(" ") and pieces:
                words.append(("".join(pieces), start, last))
                pieces = []
            if not pieces:
                start = timestamp
            pieces.append(token)
            last = timestamp
        if pieces:
            words.append(("".join(pieces), start, last))

        retained: list[Segment] = []
        final_chunk = math.isclose(chunk.keep_end, chunk.media_end, abs_tol=1e-3)
        for index, (word, begin, last_token) in enumerate(words):
            text = clean_text(word)
            if not text:
                continue
            next_start = words[index + 1][1] if index + 1 < len(words) else chunk.duration
            end = min(chunk.duration, next_start, last_token + 0.5)
            # Mehrere Tokens/Wörter können denselben Anfangszeitpunkt haben.
            end = min(chunk.duration, max(end, begin + 0.08))
            absolute_start = chunk.media_start + begin
            absolute_end = chunk.media_start + end
            if absolute_end <= absolute_start:
                continue
            midpoint = (absolute_start + absolute_end) / 2
            if midpoint < chunk.keep_start or (midpoint >= chunk.keep_end and not final_chunk):
                continue
            retained.append(Segment(absolute_start, absolute_end, text))

        # Kurze Cues erhalten Satzgrenzen; Wörter an Chunk-Rändern werden zuerst
        # nach ihrer Zeit ausgewählt, damit kein ganzer überlappender Satz wegfällt.
        cues: list[Segment] = []
        for word in retained:
            if (
                cues
                and not cues[-1].text.endswith((".", "!", "?"))
                and (word.end - cues[-1].start <= 6 and word.start - cues[-1].end < 0.8)
            ):
                previous = cues[-1]
                cues[-1] = Segment(
                    previous.start, max(previous.end, word.end), f"{previous.text} {word.text}"
                )
            else:
                cues.append(word)
        return tuple(cues)
