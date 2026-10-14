"""Gemeinsame, UI-freie Orchestrierung für einzelne Videos und Ordner."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import OUTPUT_EXTENSIONS, VIDEO_EXTENSIONS, AppConfig
from .domain import (
    BatchResult,
    JobFailure,
    JobResult,
    MutableWarnings,
    ProgressUpdate,
    Segment,
    Stage,
)
from .errors import (
    CancelledError,
    ConfigurationError,
    FileOperationError,
    LinguoAIError,
    SpeechSynthesisError,
    TranscriptionError,
)
from .media import MediaTools
from .runtime import check_cancelled, redact_secrets
from .speech import create_synthesizer
from .subtitles import render_srt
from .text import coalesce_segments, deduplicate_overlapping_segments, sanitize_segments
from .timing import (
    expand_into_free_gaps,
    merge_overfull_segments,
    rebalance_neighboring_windows,
    required_speeds,
)
from .transcription import create_transcriber
from .translation import (
    GeminiTimingOptimizer,
    GeminiTranscriptOptimizer,
    TranslationManager,
)

ProgressSink = Callable[[ProgressUpdate], None]


class VideoDubbingPipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        event_sink: ProgressSink | None = None,
        media: Any | None = None,
        transcriber: Any | None = None,
        translator: Any | None = None,
        optimizer: Any | None = None,
        timing_optimizer: Any | None = None,
        synthesizer: Any | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.config = config.validated()
        self.event_sink = event_sink or (lambda _update: None)
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._state_lock = threading.RLock()
        self._running = False
        self._last_update = ProgressUpdate(0, Stage.PREPARING, "Bereit")

        self.media = media or MediaTools(logger=self._log)
        self.transcriber = transcriber or create_transcriber(self.config, logger=self._log)
        self.translator = translator or TranslationManager(self.config, logger=self._log)
        self.optimizer = optimizer
        if self.config.optimize_transcript and self.optimizer is None:
            self.optimizer = GeminiTranscriptOptimizer(self.config, logger=self._log)
        self.timing_optimizer = timing_optimizer
        if (
            self.config.adaptive_timing
            and self.config.automatic_translation_shortening
            and self.config.gemini_api_key
            and self.timing_optimizer is None
        ):
            self.timing_optimizer = GeminiTimingOptimizer(self.config, logger=self._log)
        self.synthesizer = synthesizer or create_synthesizer(self.config, logger=self._log)

    def cancel(self) -> None:
        self.cancel_event.set()
        cancel_media = getattr(self.media, "cancel_all", None)
        if cancel_media:
            cancel_media()

    def run_path(self, input_path: str | Path, output_path: str | Path) -> BatchResult:
        with self._state_lock:
            if self._running:
                raise ConfigurationError("Diese Pipeline verarbeitet bereits einen Job.")
            if self.cancel_event.is_set():
                raise CancelledError("Die Pipeline wurde bereits abgebrochen.")
            self._running = True

        try:
            source = Path(input_path).expanduser().resolve()
            # Das Ziel absichtlich nicht dereferenzieren: Ein direkt gewählter
            # Symlink muss vor einem möglichen --overwrite erkennbar bleiben.
            target = Path(os.path.abspath(Path(output_path).expanduser()))
            if not source.exists():
                raise ConfigurationError(f"Quelle existiert nicht: {source}")
            self._preflight_services()
            if source.is_file():
                output = self._single_output_path(source, target)
                result = self._process_video(source, output, file_index=1, file_count=1)
                return BatchResult(completed=(result,))
            if source.is_dir():
                if source == target.resolve():
                    raise ConfigurationError("Quell- und Zielordner dürfen nicht identisch sein.")
                return self._process_folder(source, target)
            raise ConfigurationError(f"Quelle ist weder Datei noch Ordner: {source}")
        except OSError as exc:
            raise FileOperationError(f"Dateizugriff fehlgeschlagen: {exc}") from exc
        finally:
            self._close_services()
            with self._state_lock:
                self._running = False

    def _process_folder(self, source: Path, target: Path) -> BatchResult:
        if target.exists() and not target.is_dir():
            raise ConfigurationError("Bei Ordnerverarbeitung muss das Ziel ein Ordner sein.")
        videos = sorted(
            (
                path
                for path in source.iterdir()
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            ),
            key=lambda path: path.name.casefold(),
        )
        if not videos:
            raise ConfigurationError(f"Keine unterstützten Videos in {source} gefunden.")
        target.mkdir(parents=True, exist_ok=True)

        stem_counts = Counter(video.stem.casefold() for video in videos)
        used_output_names: set[str] = set()
        outputs: list[Path] = []
        for video in videos:
            preferred_stem = video.stem
            if stem_counts[video.stem.casefold()] > 1:
                preferred_stem = f"{video.stem}-{video.suffix.lstrip('.').lower()}"
            output_stem = preferred_stem
            collision_index = 2
            while True:
                output_name = f"{output_stem}.translated-{self.config.target_language}.mp4"
                folded_name = output_name.casefold()
                if folded_name not in used_output_names:
                    used_output_names.add(folded_name)
                    outputs.append(target / output_name)
                    break
                output_stem = f"{preferred_stem}-{collision_index}"
                collision_index += 1

        completed: list[JobResult] = []
        failed: list[JobFailure] = []
        for index, (video, output) in enumerate(zip(videos, outputs, strict=True), start=1):
            check_cancelled(self.cancel_event)
            try:
                completed.append(
                    self._process_video(video, output, file_index=index, file_count=len(videos))
                )
            except CancelledError:
                raise
            except LinguoAIError as exc:
                failed.append(JobFailure(video, str(exc)))
                self._emit_for_file(
                    100,
                    Stage.COMPLETED,
                    f"Fehler bei {video.name}: {exc}",
                    video,
                    index,
                    len(videos),
                )
        return BatchResult(tuple(completed), tuple(failed))

    def _process_video(
        self,
        source: Path,
        output: Path,
        *,
        file_index: int,
        file_count: int,
    ) -> JobResult:
        self._validate_paths(source, output)
        check_cancelled(self.cancel_event)
        output.parent.mkdir(parents=True, exist_ok=True)
        warnings = MutableWarnings()
        token = uuid.uuid4().hex
        partial_video = output.with_name(f".{output.stem}.{token}.partial{output.suffix}")
        original_srt, translated_srt = self._subtitle_paths(output)
        partial_original_srt = original_srt.with_name(f".{original_srt.name}.{token}.partial")
        partial_translated_srt = translated_srt.with_name(f".{translated_srt.name}.{token}.partial")

        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem)[:40] or "video"
        temp_root = str(self.config.temp_directory) if self.config.temp_directory else None
        lock_path: Path | None = None
        lock_token = ""
        try:
            lock_path, lock_token = self._reserve_output(output)
            with tempfile.TemporaryDirectory(
                prefix=f"linguoai-{safe_stem}-",
                dir=temp_root,
            ) as temporary:
                work = Path(temporary)
                self._emit_for_file(
                    1,
                    Stage.PREPARING,
                    f"Bereite {source.name} vor …",
                    source,
                    file_index,
                    file_count,
                )
                chunks, media_duration = self.media.extract_audio_chunks(
                    source,
                    work,
                    chunk_seconds=self.config.chunk_seconds,
                    overlap_seconds=self.config.chunk_overlap_seconds,
                    cancel_event=self.cancel_event,
                    on_chunk=lambda done, total: self._emit_for_file(
                        3 + 12 * done / total,
                        Stage.EXTRACTING,
                        f"Audio extrahiert: {done}/{total} Chunks",
                        source,
                        file_index,
                        file_count,
                    ),
                )

                transcription = self.transcriber.transcribe(
                    chunks,
                    source_language=self.config.source_language,
                    cancel_event=self.cancel_event,
                    on_progress=lambda done, total: self._emit_for_file(
                        15 + 25 * done / total,
                        Stage.TRANSCRIBING,
                        f"Transkribiert: {done}/{total} Chunks",
                        source,
                        file_index,
                        file_count,
                    ),
                )
                source_segments = deduplicate_overlapping_segments(transcription.segments)
                source_segments = sanitize_segments(
                    source_segments,
                    media_duration=media_duration,
                )
                source_segments = coalesce_segments(source_segments)
                if not source_segments:
                    raise TranscriptionError(
                        "Nach der Zeitbereinigung blieben keine Segmente übrig."
                    )

                if self.config.optimize_transcript:
                    source_segments = self.optimizer.optimize(
                        source_segments,
                        cancel_event=self.cancel_event,
                        on_progress=lambda done, total: self._emit_for_file(
                            40 + 8 * done / total,
                            Stage.OPTIMIZING,
                            f"Text optimiert: {done}/{total} Batches",
                            source,
                            file_index,
                            file_count,
                        ),
                    )
                else:
                    self._emit_for_file(
                        48,
                        Stage.OPTIMIZING,
                        "Optionale Textoptimierung übersprungen",
                        source,
                        file_index,
                        file_count,
                    )

                translated_segments = self.translator.translate_segments(
                    source_segments,
                    source_language=self.config.source_language or transcription.language,
                    cancel_event=self.cancel_event,
                    on_progress=lambda done, total: self._emit_for_file(
                        48 + 14 * done / total,
                        Stage.TRANSLATING,
                        f"Übersetzt: {done}/{total} Batches",
                        source,
                        file_index,
                        file_count,
                    ),
                )

                if self.config.tts_engine == "chatterbox":
                    self._release_upstream_models_for_tts()

                raw_clips = self.synthesizer.synthesize(
                    translated_segments,
                    work / "tts",
                    cancel_event=self.cancel_event,
                    on_progress=lambda done, total: self._emit_for_file(
                        62 + 13 * done / total,
                        Stage.SYNTHESIZING,
                        f"Sprache erzeugt: {done}/{total} Segmente",
                        source,
                        file_index,
                        file_count,
                    ),
                )

                if self.config.adaptive_timing and hasattr(self.media, "probe_duration"):
                    translated_segments, raw_clips = self._adapt_speech_timing(
                        translated_segments,
                        raw_clips,
                        work=work,
                        media_duration=media_duration,
                        source=source,
                        file_index=file_index,
                        file_count=file_count,
                        warnings=warnings,
                    )

                normalized_directory = work / "normalized"
                normalized_directory.mkdir(parents=True, exist_ok=True)
                normalized_clips: list[Path] = []
                for index, (segment, raw_clip) in enumerate(
                    zip(translated_segments, raw_clips, strict=True),
                    start=1,
                ):
                    normalized = normalized_directory / f"clip-{index - 1:05d}.wav"
                    try:
                        normalization = self.media.normalize_voice_clip(
                            raw_clip,
                            normalized,
                            slot_duration=segment.duration,
                            maximum_speed=self.config.max_tts_speed,
                            cancel_event=self.cancel_event,
                        )
                    except SpeechSynthesisError as exc:
                        raise SpeechSynthesisError(f"Segment {index}: {exc}") from exc
                    if normalization.applied_speed > 1.5:
                        warning = (
                            f"Segment {index} wurde auf {normalization.applied_speed:.2f}x "
                            "beschleunigt."
                        )
                        warnings.add(warning)
                        self._log(warning)
                    normalized_clips.append(normalized)
                    self._emit_for_file(
                        75 + 10 * index / len(raw_clips),
                        Stage.SYNCHRONIZING,
                        f"Audio eingepasst: {index}/{len(raw_clips)} Segmente",
                        source,
                        file_index,
                        file_count,
                    )

                final_audio = work / "dubbed-timeline.wav"
                self.media.build_timeline(
                    translated_segments,
                    normalized_clips,
                    final_audio,
                    media_duration=media_duration,
                    cancel_event=self.cancel_event,
                )
                self._emit_for_file(
                    90,
                    Stage.MUXING,
                    "Füge Video und neue Tonspur zusammen …",
                    source,
                    file_index,
                    file_count,
                )
                self.media.mux_video(
                    source,
                    final_audio,
                    partial_video,
                    media_duration=media_duration,
                    target_language=self.config.target_language,
                    cancel_event=self.cancel_event,
                )

                partial_original_srt.write_text(
                    render_srt(source_segments), encoding="utf-8", newline="\n"
                )
                partial_translated_srt.write_text(
                    render_srt(translated_segments), encoding="utf-8", newline="\n"
                )
                check_cancelled(self.cancel_event)
                self._publish_artifacts(
                    (
                        (partial_original_srt, original_srt),
                        (partial_translated_srt, translated_srt),
                        # Das Video ist der Abschluss-Marker und wird zuletzt sichtbar.
                        (partial_video, output),
                    )
                )

                self._emit_for_file(
                    100,
                    Stage.COMPLETED,
                    f"Fertig: {output.name}",
                    source,
                    file_index,
                    file_count,
                )
                return JobResult(
                    input_path=source,
                    output_path=output,
                    original_subtitles=original_srt,
                    translated_subtitles=translated_srt,
                    source_language=transcription.language,
                    segment_count=len(translated_segments),
                    warnings=tuple(warnings.items),
                )
        except OSError as exc:
            raise FileOperationError(
                f"Dateizugriff für {output.name} fehlgeschlagen: {exc}"
            ) from exc
        finally:
            for partial in (partial_video, partial_original_srt, partial_translated_srt):
                try:
                    partial.unlink(missing_ok=True)
                except OSError as exc:
                    self._log(f"Warnung: Temporäre Datei blieb zurück ({partial.name}): {exc}")
            if lock_path is not None:
                self._release_output(lock_path, lock_token)

    def _validate_paths(self, source: Path, output: Path) -> None:
        if source.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ConfigurationError(f"Nicht unterstütztes Videoformat: {source.suffix}")
        if output.suffix.lower() not in OUTPUT_EXTENSIONS:
            raise ConfigurationError(
                "Das Ausgabeformat muss .mp4, .mkv oder .mov sein; empfohlen ist .mp4."
            )
        if source.resolve() == output.resolve():
            raise ConfigurationError("Quelldatei und Zieldatei dürfen nicht identisch sein.")
        original_srt, translated_srt = self._subtitle_paths(output)
        targets = (output, original_srt, translated_srt)
        for path in targets:
            self._replaceable_target_stat(path)
        conflicts = [path for path in targets if path.exists()]
        if conflicts and not self.config.overwrite:
            raise ConfigurationError(
                "Zieldatei existiert bereits (nutze --overwrite): "
                + ", ".join(path.name for path in conflicts)
            )

    def _single_output_path(self, source: Path, target: Path) -> Path:
        if target.exists() and target.is_dir():
            return target / f"{source.stem}.translated-{self.config.target_language}.mp4"
        if not target.suffix:
            return target.with_suffix(".mp4")
        return target

    @staticmethod
    def _lock_path(output: Path) -> Path:
        # Video und beide SRTs bilden eine Ergebnismenge. Verschiedene
        # Video-Endungen mit demselben Stem teilen daher dieselbe Sperre.
        return output.with_name(f".{output.stem}.linguoai.lock")

    def _reserve_output(self, output: Path) -> tuple[Path, str]:
        lock_path = self._lock_path(output)
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ConfigurationError(
                f"Ein anderer LinguoAI-Job reserviert bereits {output.name}. "
                f"Falls kein Job läuft, entferne die verwaiste Sperrdatei {lock_path.name}."
            ) from exc
        except OSError as exc:
            raise FileOperationError(f"Ziel konnte nicht reserviert werden: {exc}") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            lock_path.unlink(missing_ok=True)
            raise FileOperationError(f"Zielsperre konnte nicht geschrieben werden: {exc}") from exc
        return lock_path, token

    def _release_output(self, lock_path: Path, token: str) -> None:
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink(missing_ok=True)
        except FileNotFoundError:
            return
        except OSError as exc:
            self._log(f"Warnung: Zielsperre konnte nicht entfernt werden: {exc}")

    def _publish_artifacts(self, artifacts: tuple[tuple[Path, Path], ...]) -> None:
        """Veröffentlicht Video und Untertitel als rollback-fähige Ergebnismenge."""
        if self.config.overwrite:
            self._publish_with_backups(artifacts)
            return

        attempted: dict[Path, os.stat_result] = {}
        try:
            for partial, target in artifacts:
                attempted[target] = partial.stat()
                self._publish_without_replacement(partial, target)
        except FileExistsError as exc:
            rollback_errors = self._remove_owned_targets(attempted)
            detail = ""
            if rollback_errors:
                detail = " Rollback unvollständig: " + " | ".join(rollback_errors)
            conflict = str(exc.filename or "unbekanntes Ziel")
            raise ConfigurationError(
                f"Eine Zieldatei wurde zwischenzeitlich erstellt: {conflict}.{detail}"
            ) from exc
        except BaseException as exc:
            rollback_errors = self._remove_owned_targets(attempted)
            if isinstance(exc, OSError):
                detail = ""
                if rollback_errors:
                    detail = " Rollback unvollständig: " + " | ".join(rollback_errors)
                raise FileOperationError(
                    f"Ausgaben konnten nicht atomar veröffentlicht werden.{detail}"
                ) from exc
            if rollback_errors:
                self._log(
                    "Warnung: Rollback nach Abbruch unvollständig: " + " | ".join(rollback_errors)
                )
            raise

    @staticmethod
    def _publish_without_replacement(partial: Path, target: Path) -> None:
        """Bevorzugt einen atomaren Hardlink, mit exklusivem Portable-Fallback."""
        try:
            os.link(partial, target)
        except FileExistsError:
            raise
        except OSError:
            # Manche Netzwerk-/Wechseldatenträger erlauben keine Hardlinks. Eine
            # exklusiv erstellte Platzhalterdatei reserviert trotzdem den Namen.
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.close(descriptor)
                descriptor = -1
                os.replace(partial, target)
            except BaseException:
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
                with suppress(OSError):
                    target.unlink(missing_ok=True)
                raise
        else:
            with suppress(OSError):
                partial.unlink()

    def _publish_with_backups(self, artifacts: tuple[tuple[Path, Path], ...]) -> None:
        backup_token = uuid.uuid4().hex
        backups: dict[Path, Path] = {}
        attempted: dict[Path, os.stat_result] = {}
        try:
            # Die Prüfung wird unmittelbar vor dem Ersetzen wiederholt. So kann
            # ein während des langen Jobs angelegtes Verzeichnis/Symlink nicht
            # versehentlich verschoben werden.
            for _partial, target in artifacts:
                self._replaceable_target_stat(target)
            for _partial, target in artifacts:
                if self._replaceable_target_stat(target) is not None:
                    backup = target.with_name(f".{target.name}.{backup_token}.backup")
                    if backup.exists() or backup.is_symlink():
                        raise FileOperationError(
                            f"Temporäre Sicherung existiert bereits: {backup.name}"
                        )
                    # Absicht vor dem Syscall erfassen, damit auch ein Signal direkt
                    # nach erfolgreichem os.replace() rückgängig gemacht werden kann.
                    backups[target] = backup
                    os.replace(target, backup)
                    self._replaceable_target_stat(backup)
            for partial, target in artifacts:
                if self._replaceable_target_stat(target) is not None:
                    raise ConfigurationError(
                        f"Ausgabeziel wurde während des Jobs neu erstellt: {target.name}"
                    )
                attempted[target] = partial.stat()
                os.replace(partial, target)
                if not self._matches_stat(target, attempted[target]):
                    raise FileOperationError(f"Ausgabeziel änderte sich unerwartet: {target.name}")
        except BaseException as exc:
            rollback_errors = self._remove_owned_targets(attempted)
            for target, backup in reversed(tuple(backups.items())):
                if not (backup.exists() or backup.is_symlink()):
                    continue
                try:
                    if target.exists() or target.is_symlink():
                        rollback_errors.append(
                            f"{target.name}: zwischenzeitlich neu belegt; "
                            f"Sicherung bleibt als {backup.name} erhalten"
                        )
                        continue
                    os.replace(backup, target)
                except OSError as rollback_error:
                    rollback_errors.append(f"{backup.name}: {rollback_error}")
            detail = ""
            if rollback_errors:
                detail = " Rollback unvollständig: " + " | ".join(rollback_errors)
            if isinstance(exc, OSError):
                raise FileOperationError(
                    f"Ausgaben konnten nicht als zusammengehörige Gruppe ersetzt werden.{detail}"
                ) from exc
            if rollback_errors:
                self._log(
                    "Warnung: Rollback nach Abbruch unvollständig: " + " | ".join(rollback_errors)
                )
            raise
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError as exc:
                self._log(f"Warnung: Sicherung blieb zurück ({backup.name}): {exc}")

    @staticmethod
    def _replaceable_target_stat(path: Path) -> os.stat_result | None:
        """Akzeptiert nur fehlende oder reguläre Ziele, ohne Symlinks zu folgen."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ConfigurationError(
                f"Ausgabeziele dürfen weder Verzeichnisse noch symbolische Links sein: {path.name}"
            )
        return info

    @staticmethod
    def _matches_stat(path: Path, expected: os.stat_result) -> bool:
        try:
            return os.path.samestat(path.lstat(), expected)
        except OSError:
            return False

    def _remove_owned_targets(
        self,
        attempted: dict[Path, os.stat_result],
    ) -> list[str]:
        errors: list[str] = []
        for target, expected in reversed(tuple(attempted.items())):
            if not self._matches_stat(target, expected):
                continue
            try:
                target.unlink()
            except OSError as exc:
                errors.append(f"{target.name}: {exc}")
        return errors

    @staticmethod
    def _subtitle_paths(output: Path) -> tuple[Path, Path]:
        base = output.with_suffix("")
        return (
            Path(f"{base}.original.srt"),
            Path(f"{base}.translated.srt"),
        )

    def _emit_for_file(
        self,
        local_percent: float,
        stage: Stage,
        message: str,
        input_path: Path,
        file_index: int,
        file_count: int,
    ) -> None:
        global_percent = ((file_index - 1) + local_percent / 100) / file_count * 100
        self._emit(
            global_percent,
            stage,
            message,
            input_path=input_path,
            file_index=file_index,
            file_count=file_count,
        )

    def _emit(
        self,
        percent: float,
        stage: Stage,
        message: str,
        *,
        input_path: Path | None = None,
        file_index: int = 1,
        file_count: int = 1,
    ) -> None:
        update = ProgressUpdate(
            percent=percent,
            stage=stage,
            message=message,
            input_path=input_path,
            file_index=file_index,
            file_count=file_count,
        )
        with self._state_lock:
            self._last_update = update
        self.event_sink(update)

    def _log(self, message: str) -> None:
        with self._state_lock:
            previous = self._last_update
        self.event_sink(
            ProgressUpdate(
                percent=previous.percent,
                stage=previous.stage,
                message=message,
                input_path=previous.input_path,
                file_index=previous.file_index,
                file_count=previous.file_count,
            )
        )

    def _adapt_speech_timing(
        self,
        segments: Sequence[Segment],
        clips: Sequence[Path],
        *,
        work: Path,
        media_duration: float,
        source: Path,
        file_index: int,
        file_count: int,
        warnings: MutableWarnings,
    ) -> tuple[list[Segment], list[Path]]:
        """Nutzt Stille, Nachbarfenster, Cue-Bündelung und optionale Textkürzung."""
        current_segments = list(segments)
        current_clips = list(clips)
        durations = self._probe_clip_durations(current_clips)
        current_segments = self._optimize_segment_windows(
            current_segments,
            durations,
            media_duration=media_duration,
        )

        speeds = required_speeds(current_segments, durations)
        if speeds and max(speeds) > self.config.preferred_tts_speed + 1e-3:
            merged = merge_overfull_segments(
                current_segments,
                durations,
                preferred_speed=self.config.preferred_tts_speed,
            )
            if merged.changed:
                self._log(
                    f"Adaptive Synchronisierung: {merged.merge_count} benachbarte "
                    "Segmentgruppe(n) zusammengeführt."
                )
                current_segments = list(merged.segments)
                current_clips = self._resynthesize_for_timing(
                    current_segments,
                    work / "tts-adaptive-merged",
                )
                durations = self._probe_clip_durations(current_clips)
                current_segments = self._optimize_segment_windows(
                    current_segments,
                    durations,
                    media_duration=media_duration,
                )

        for pass_number in range(1, 3):
            speeds = required_speeds(current_segments, durations)
            if not speeds or max(speeds) <= self.config.preferred_tts_speed + 1e-3:
                break
            if self.timing_optimizer is None:
                break
            shortened, changed = self.timing_optimizer.shorten(
                current_segments,
                durations,
                preferred_speed=self.config.preferred_tts_speed,
                cancel_event=self.cancel_event,
            )
            if not changed:
                break
            self._log(
                f"Adaptive Synchronisierung: {len(changed)} zu lange Segment(e) "
                f"inhaltstreu gekürzt (Durchlauf {pass_number})."
            )
            current_segments = shortened
            current_clips = self._resynthesize_for_timing(
                current_segments,
                work / f"tts-adaptive-shortened-{pass_number}",
            )
            durations = self._probe_clip_durations(current_clips)
            current_segments = self._optimize_segment_windows(
                current_segments,
                durations,
                media_duration=media_duration,
            )

        speeds = required_speeds(current_segments, durations)
        if speeds:
            worst = max(speeds)
            worst_index = speeds.index(worst) + 1
            if worst > self.config.preferred_tts_speed + 1e-3:
                message = (
                    f"Segment {worst_index} benötigt nach der intelligenten Zeitoptimierung "
                    f"noch {worst:.2f}x Tempo."
                )
                warnings.add(message)
                self._log(message)
        self._emit_for_file(
            75,
            Stage.SYNCHRONIZING,
            "Freie Pausen und Nachbarsegmente intelligent verteilt",
            source,
            file_index,
            file_count,
        )
        return current_segments, current_clips

    def _optimize_segment_windows(
        self,
        segments: Sequence[Segment],
        audio_durations: Sequence[float],
        *,
        media_duration: float,
    ) -> list[Segment]:
        expanded = expand_into_free_gaps(
            segments,
            audio_durations,
            media_duration=media_duration,
        )
        current = list(expanded.segments)
        if expanded.changed:
            self._log(
                f"Adaptive Synchronisierung: {expanded.borrowed_seconds:.2f}s freie Pause "
                "für Sprachsegmente genutzt."
            )
        rebalanced = rebalance_neighboring_windows(
            current,
            audio_durations,
            preferred_speed=self.config.preferred_tts_speed,
        )
        if rebalanced.changed:
            self._log(
                f"Adaptive Synchronisierung: {rebalanced.shifted_boundaries} "
                "Segmentgrenze(n) neu verteilt."
            )
        return list(rebalanced.segments)

    def _probe_clip_durations(self, clips: Sequence[Path]) -> list[float]:
        durations: list[float] = []
        for clip in clips:
            check_cancelled(self.cancel_event)
            durations.append(self.media.probe_duration(clip, self.cancel_event))
        return durations

    def _resynthesize_for_timing(
        self,
        segments: Sequence[Segment],
        output_directory: Path,
    ) -> list[Path]:
        check_cancelled(self.cancel_event)
        return list(
            self.synthesizer.synthesize(
                segments,
                output_directory,
                cancel_event=self.cancel_event,
            )
        )

    def _close_services(self) -> None:
        for service in (
            self.transcriber,
            self.translator,
            self.optimizer,
            self.timing_optimizer,
            self.synthesizer,
        ):
            self._close_service(service)

    def _release_upstream_models_for_tts(self) -> None:
        """Verhindert, dass ASR/NLLB gleichzeitig mit Chatterbox GPU/RAM belegen."""
        self._log("Gebe Spracherkennung und lokale Übersetzungsmodelle vor Chatterbox frei …")
        self._close_service(self.transcriber)
        required_services = {self.config.translation_service, *self.config.fallback_services}
        if "nllb" in required_services:
            self._close_service(self.translator)

    def _close_service(self, service: Any) -> None:
        close = getattr(service, "close", None)
        if not close:
            return
        try:
            close()
        except Exception as exc:
            safe_error = redact_secrets(
                exc,
                self.config.deepl_api_key,
                self.config.gemini_api_key,
                os.environ.get("HF_TOKEN", ""),
                os.environ.get("HUGGING_FACE_HUB_TOKEN", ""),
            )
            self._log(f"Warnung beim Schließen eines Dienstes: {safe_error[:300]}")

    def _preflight_services(self) -> None:
        for service in (
            self.transcriber,
            self.translator,
            self.optimizer,
            self.timing_optimizer,
            self.synthesizer,
        ):
            preflight = getattr(service, "preflight", None)
            if preflight:
                preflight()
