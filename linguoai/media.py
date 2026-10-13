"""FFmpeg-Anbindung und samplegenaue PCM-Zeitachse."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .domain import AudioChunk, Segment
from .errors import CancelledError, DependencyError, ExternalToolError, SpeechSynthesisError
from .runtime import check_cancelled

_SECRET_ENVIRONMENT_KEYS = {
    "DEEPL_API_KEY",
    "DEEPL_AUTH_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
}


class CommandRunner:
    """Thread-sicherer, abbrechbarer Subprocess-Runner ohne ``shell=True``."""

    def __init__(self) -> None:
        self._active: set[subprocess.Popen[str]] = set()
        self._lock = threading.RLock()

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        cancel_event: threading.Event,
    ) -> tuple[str, str]:
        check_cancelled(cancel_event)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW

        environment = os.environ.copy()
        for key in _SECRET_ENVIRONMENT_KEYS:
            environment.pop(key, None)

        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
                env=environment,
            )
            with self._lock:
                self._active.add(process)
            started = time.monotonic()
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event.is_set():
                        self._stop(process)
                        raise CancelledError("Verarbeitung wurde abgebrochen.") from None
                    if time.monotonic() - started > timeout:
                        self._stop(process)
                        raise ExternalToolError(
                            f"Zeitüberschreitung nach {timeout:.0f}s: {Path(command[0]).name}"
                        ) from None

            check_cancelled(cancel_event)
            if process.returncode != 0:
                detail = (stderr or stdout or "Keine Fehlerdetails").strip()[-4_000:]
                raise ExternalToolError(
                    f"{Path(command[0]).name} endete mit Code {process.returncode}:\n{detail}"
                )
            return stdout, stderr
        except FileNotFoundError as exc:
            raise DependencyError(f"Programm nicht gefunden: {command[0]}") from exc
        except BaseException:
            # Auch KeyboardInterrupt/SystemExit dürfen keinen FFmpeg-Prozess verwaisen lassen.
            if process is not None:
                self._stop(process)
            raise
        finally:
            if process is not None:
                with self._lock:
                    self._active.discard(process)

    def cancel_all(self) -> None:
        with self._lock:
            processes = tuple(self._active)
        for process in processes:
            self._stop(process)

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    original_duration: float
    slot_duration: float
    required_speed: float
    applied_speed: float


def atempo_chain(speed: float) -> list[float]:
    """Zerlegt einen Tempofaktor in FFmpeg-kompatible Faktoren von 0,5 bis 2,0."""
    if speed <= 0 or not math.isfinite(speed):
        raise ValueError("Tempo muss positiv und endlich sein")
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    if not math.isclose(remaining, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        factors.append(remaining)
    return factors


class MediaTools:
    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    CHANNELS = 1

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        logger: Callable[[str], None] | None = None,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.logger = logger or (lambda _message: None)
        self.ffmpeg = self._find_executable(ffmpeg, "ffmpeg")
        self.ffprobe = self._find_executable(ffprobe, "ffprobe")

    @staticmethod
    def _find_executable(explicit: str | None, name: str) -> str:
        if explicit:
            return explicit
        executable = f"{name}.exe" if os.name == "nt" else name
        bundled = Path(__file__).resolve().parent.parent / "ffmpeg" / executable
        if bundled.is_file():
            return str(bundled)
        discovered = shutil.which(name)
        if discovered:
            return discovered
        raise DependencyError(
            f"{name} wurde nicht gefunden. Installiere FFmpeg und ergänze "
            "dessen bin-Ordner im PATH."
        )

    def cancel_all(self) -> None:
        self.runner.cancel_all()

    def probe_duration(self, path: Path, cancel_event: threading.Event) -> float:
        stdout, _ = self.runner.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            timeout=30,
            cancel_event=cancel_event,
        )
        try:
            duration = float(stdout.strip())
        except ValueError as exc:
            raise ExternalToolError(f"Keine gültige Mediendauer für {path.name}") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ExternalToolError(f"Ungültige Mediendauer für {path.name}: {duration}")
        return duration

    def extract_audio_chunks(
        self,
        video_path: Path,
        work_directory: Path,
        *,
        chunk_seconds: float,
        overlap_seconds: float,
        cancel_event: threading.Event,
        on_chunk: Callable[[int, int], None] | None = None,
    ) -> tuple[list[AudioChunk], float]:
        duration = self.probe_duration(video_path, cancel_event)
        count = math.ceil(duration / chunk_seconds)
        chunks: list[AudioChunk] = []
        chunk_directory = work_directory / "asr"
        chunk_directory.mkdir(parents=True, exist_ok=True)

        for index in range(count):
            check_cancelled(cancel_event)
            keep_start = index * chunk_seconds
            keep_end = min(duration, keep_start + chunk_seconds)
            if keep_end - keep_start <= 1e-6:
                continue
            media_start = max(0.0, keep_start - (overlap_seconds if index else 0.0))
            media_end = min(
                duration,
                keep_end + (overlap_seconds if keep_end < duration else 0.0),
            )
            output = chunk_directory / f"chunk-{index:05d}.wav"
            self.runner.run(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{media_start:.6f}",
                    "-i",
                    str(video_path),
                    "-t",
                    f"{media_end - media_start:.6f}",
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ],
                timeout=max(120.0, (media_end - media_start) * 3.0),
                cancel_event=cancel_event,
            )
            if not output.is_file():
                raise ExternalToolError(f"Audio-Chunk {index + 1} wurde nicht korrekt erzeugt.")
            try:
                with wave.open(str(output), "rb") as reader:
                    frames = reader.getnframes()
            except (EOFError, wave.Error) as exc:
                raise ExternalToolError(
                    f"Audio-Chunk {index + 1} ist keine gültige WAV-Datei."
                ) from exc
            if frames <= 0:
                output.unlink(missing_ok=True)
                if on_chunk:
                    on_chunk(index + 1, count)
                continue
            chunks.append(
                AudioChunk(
                    index=index,
                    path=output,
                    media_start=media_start,
                    media_end=media_end,
                    keep_start=keep_start,
                    keep_end=keep_end,
                )
            )
            if on_chunk:
                on_chunk(index + 1, count)
        if not chunks:
            raise ExternalToolError("Die Eingabedatei enthält keine extrahierbare Audiospur.")
        return chunks, duration

    def normalize_voice_clip(
        self,
        source: Path,
        destination: Path,
        *,
        slot_duration: float,
        maximum_speed: float,
        cancel_event: threading.Event,
    ) -> NormalizationResult:
        if slot_duration <= 0:
            raise SpeechSynthesisError("TTS-Zeitfenster ist nicht positiv.")
        audio_duration = self.probe_duration(source, cancel_event)
        required_speed = audio_duration / slot_duration
        if required_speed > maximum_speed + 1e-3:
            raise SpeechSynthesisError(
                "Ein Sprachsegment passt nicht in sein Zeitfenster: "
                f"benötigt {required_speed:.2f}x, erlaubt sind höchstens {maximum_speed:.2f}x."
            )
        applied_speed = max(1.0, required_speed)
        filters = [f"atempo={factor:.8f}" for factor in atempo_chain(applied_speed)]
        filters.extend(
            [
                f"aresample={self.SAMPLE_RATE}",
                "aformat=sample_fmts=s16:channel_layouts=mono",
                "apad",
                f"atrim=duration={slot_duration:.6f}",
            ]
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-af",
                ",".join(filters),
                "-ac",
                str(self.CHANNELS),
                "-ar",
                str(self.SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            timeout=max(60.0, audio_duration * 4.0),
            cancel_event=cancel_event,
        )
        self._validate_pcm_wave(destination)
        return NormalizationResult(
            original_duration=audio_duration,
            slot_duration=slot_duration,
            required_speed=required_speed,
            applied_speed=applied_speed,
        )

    def build_timeline(
        self,
        segments: Sequence[Segment],
        clips: Sequence[Path],
        destination: Path,
        *,
        media_duration: float,
        cancel_event: threading.Event,
    ) -> None:
        if len(segments) != len(clips):
            raise SpeechSynthesisError("Segment- und TTS-Anzahl stimmen nicht überein.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        total_frames = round(media_duration * self.SAMPLE_RATE)
        current_frame = 0

        with wave.open(str(destination), "wb") as output:
            output.setnchannels(self.CHANNELS)
            output.setsampwidth(self.SAMPLE_WIDTH)
            output.setframerate(self.SAMPLE_RATE)
            for segment, clip_path in zip(segments, clips, strict=True):
                check_cancelled(cancel_event)
                target_start = round(segment.start * self.SAMPLE_RATE)
                target_end = round(segment.end * self.SAMPLE_RATE)
                if target_start < current_frame:
                    raise SpeechSynthesisError("Überlappende Segmente in der Audio-Zeitachse.")
                self._write_silence(
                    output,
                    target_start - current_frame,
                    cancel_event=cancel_event,
                )
                expected_frames = target_end - target_start
                with wave.open(str(clip_path), "rb") as clip:
                    self._validate_wave_reader(clip, clip_path)
                    data = clip.readframes(expected_frames)
                actual_frames = len(data) // (self.SAMPLE_WIDTH * self.CHANNELS)
                output.writeframesraw(data)
                self._write_silence(
                    output,
                    expected_frames - actual_frames,
                    cancel_event=cancel_event,
                )
                current_frame = target_end
            self._write_silence(
                output,
                max(0, total_frames - current_frame),
                cancel_event=cancel_event,
            )

        self._validate_pcm_wave(destination)

    def mux_video(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        *,
        media_duration: float,
        target_language: str,
        cancel_event: threading.Event,
    ) -> None:
        common = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-metadata:s:a:0",
            f"language={target_language}",
            "-t",
            f"{media_duration:.6f}",
        ]
        tail: list[str] = []
        if output_path.suffix.lower() in {".mp4", ".mov"}:
            tail.extend(["-movflags", "+faststart"])
        timeout = max(180.0, media_duration * 4.0)

        try:
            self.runner.run(
                [*common, "-c:v", "copy", *tail, str(output_path)],
                timeout=timeout,
                cancel_event=cancel_event,
            )
        except ExternalToolError as copy_error:
            output_path.unlink(missing_ok=True)
            self.logger(
                "Videostream konnte nicht kopiert werden; falle auf H.264-Software-Encoding zurück."
            )
            try:
                self.runner.run(
                    [
                        *common,
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "18",
                        *tail,
                        str(output_path),
                    ],
                    timeout=max(300.0, media_duration * 8.0),
                    cancel_event=cancel_event,
                )
            except ExternalToolError as encode_error:
                raise ExternalToolError(
                    f"Video-Muxing schlug als Stream-Copy und als H.264 fehl. "
                    f"Stream-Copy: {copy_error}; H.264: {encode_error}"
                ) from encode_error

        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ExternalToolError("FFmpeg hat keine gültige Ausgabedatei erzeugt.")
        output_duration = self.probe_duration(output_path, cancel_event)
        if output_duration < media_duration - 1.0:
            raise ExternalToolError(
                f"Ausgabe ist unvollständig ({output_duration:.2f}s statt {media_duration:.2f}s)."
            )

    @classmethod
    def _validate_pcm_wave(cls, path: Path) -> None:
        try:
            with wave.open(str(path), "rb") as reader:
                cls._validate_wave_reader(reader, path)
                if reader.getnframes() <= 0:
                    raise SpeechSynthesisError(f"Leere WAV-Datei: {path.name}")
        except (EOFError, wave.Error) as exc:
            raise SpeechSynthesisError(f"Ungültige WAV-Datei: {path.name}") from exc

    @classmethod
    def _validate_wave_reader(cls, reader: wave.Wave_read, path: Path) -> None:
        actual = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
        expected = (cls.CHANNELS, cls.SAMPLE_WIDTH, cls.SAMPLE_RATE)
        if actual != expected or reader.getcomptype() != "NONE":
            raise SpeechSynthesisError(
                f"Inkompatibles PCM-Format in {path.name}: {actual}, erwartet {expected}."
            )

    @classmethod
    def _write_silence(
        cls,
        output: wave.Wave_write,
        frame_count: int,
        *,
        cancel_event: threading.Event,
    ) -> None:
        if frame_count <= 0:
            return
        bytes_per_frame = cls.SAMPLE_WIDTH * cls.CHANNELS
        block_frames = 16_384
        full_block = b"\x00" * (block_frames * bytes_per_frame)
        remaining = frame_count
        while remaining:
            check_cancelled(cancel_event)
            count = min(block_frames, remaining)
            output.writeframesraw(full_block[: count * bytes_per_frame])
            remaining -= count
