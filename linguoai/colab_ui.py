"""Geschützte Gradio-Oberfläche für Google Colab und lokale Browser-Sitzungen."""

from __future__ import annotations

import atexit
import inspect
import math
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .config import (
    ASR_ENGINES,
    LANGUAGES,
    PARAKEET_PROFILES,
    TTS_DEVICES,
    TTS_ENGINES,
    VIDEO_EXTENSIONS,
    AppConfig,
)
from .domain import ProgressUpdate
from .errors import CancelledError, LinguoAIError
from .pipeline import VideoDubbingPipeline
from .runtime import check_cancelled

COLAB_ROOT = Path("/content")
DRIVE_ROOT = COLAB_ROOT / "drive" / "MyDrive"
JOB_ROOT = COLAB_ROOT / "linguoai_jobs"
OUTPUT_ROOT = COLAB_ROOT / "linguoai_outputs"
OUTPUT_RETENTION_SECONDS = 6 * 60 * 60
AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
CLOUDFLARE_TUNNEL_TOKEN_ENV = "CLOUDFLARE_TUNNEL_TOKEN"
CLOUDFLARE_TUNNEL_HOSTNAME_ENV = "CLOUDFLARE_TUNNEL_HOSTNAME"
DEFAULT_GRADIO_PORT = 7860
DEFAULT_UPLOAD_LIMIT_MB = 5000
DEFAULT_CLOUDFLARE_UPLOAD_LIMIT_MB = 95
DEFAULT_TUNNEL_STARTUP_TIMEOUT = 30.0
_active_tunnel_process: subprocess.Popen[str] | None = None


def resolve_input_video(
    uploaded_video: str | None,
    drive_path: str,
    *,
    drive_root: Path = DRIVE_ROOT,
) -> Path:
    """Wählt Upload vor Drive und verhindert Pfadausbruch aus ``MyDrive``."""
    if uploaded_video:
        candidate = Path(uploaded_video).expanduser().resolve()
    else:
        value = drive_path.strip()
        if not value:
            raise ValueError("Lade ein Video hoch oder gib einen Google-Drive-Pfad an.")
        root = drive_root.expanduser().resolve()
        supplied = Path(value).expanduser()
        candidate = (supplied if supplied.is_absolute() else root / supplied).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Der Drive-Pfad muss innerhalb von MyDrive liegen.")
    if not candidate.is_file():
        raise ValueError(f"Videodatei wurde nicht gefunden: {candidate}")
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"Nicht unterstütztes Videoformat: {candidate.suffix}")
    return candidate


def resolve_drive_output_directory(
    relative_folder: str,
    *,
    drive_root: Path = DRIVE_ROOT,
) -> Path:
    """Erlaubt für Ausgaben ausschließlich relative Ordner unter ``MyDrive``."""
    folder = Path(relative_folder.strip() or "LinguoAI Outputs")
    if folder.is_absolute() or ".." in folder.parts:
        raise ValueError("Der Drive-Ausgabeordner muss relativ zu MyDrive sein.")
    root = drive_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Google Drive ist nicht unter /content/drive eingebunden.")
    destination = (root / folder).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("Der Drive-Ausgabeordner liegt außerhalb von MyDrive.")
    return destination


def resolve_optional_job_file(
    uploaded_file: str | None,
    drive_path: str,
    *,
    label: str,
    allowed_extensions: frozenset[str],
    required: bool = False,
    drive_root: Path = DRIVE_ROOT,
) -> Path | None:
    """Wählt Upload vor Drive und begrenzt Drive-Dateien auf ``MyDrive``."""
    if uploaded_file:
        candidate = Path(uploaded_file).expanduser().resolve()
    else:
        value = drive_path.strip()
        if not value:
            if required:
                raise ValueError(f"{label}: Lade eine Datei hoch oder gib einen Drive-Pfad an.")
            return None
        root = drive_root.expanduser().resolve()
        supplied = Path(value).expanduser()
        candidate = (supplied if supplied.is_absolute() else root / supplied).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"{label}: Der Drive-Pfad muss innerhalb von MyDrive liegen.")
    if not candidate.is_file():
        raise ValueError(f"{label} wurde nicht gefunden: {candidate}")
    if candidate.suffix.casefold() not in allowed_extensions:
        formats = ", ".join(sorted(allowed_extensions))
        raise ValueError(f"{label}: Dateiformat {candidate.suffix!r} ist ungültig ({formats}).")
    return candidate


def process_colab_job(
    uploaded_video: str | None,
    drive_path: str,
    target_language: str,
    source_language: str,
    translation_service: str,
    whisper_model: str,
    device: str,
    nllb_device: str,
    nllb_model: str,
    nllb_revision: str,
    nllb_batch_size: int,
    nllb_source_code: str,
    nllb_target_code: str,
    tts_voice: str,
    deepl_api_key: str,
    gemini_api_key: str,
    optimize_transcript: bool,
    save_to_drive: bool,
    drive_output_folder: str,
    *,
    asr_engine: str = "whisper",
    parakeet_model_directory: str = "",
    parakeet_profile: str = "multilingual-v3",
    parakeet_threads: int = 4,
    tts_engine: str = "edge",
    tts_device: str = "auto",
    uploaded_tts_reference: str | None = None,
    tts_reference_drive_path: str = "",
    confirm_voice_rights: bool = False,
    uploaded_piper_model: str | None = None,
    piper_model_drive_path: str = "",
    uploaded_piper_config: str | None = None,
    piper_config_drive_path: str = "",
    piper_speaker_id: int | float | None = None,
    piper_length_scale: float = 1.0,
    chatterbox_model: str = "v2",
    chatterbox_exaggeration: float = 0.5,
    chatterbox_cfg_weight: float = 0.5,
    fallback_to_google: bool = True,
    request_retries: int = 4,
    request_retry_base_seconds: float = 2.0,
    gemini_request_pause_seconds: float = 2.5,
    progress_callback: Callable[[float, str], None] | None = None,
    pipeline_ready: Callable[[VideoDubbingPipeline | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, list[str]]:
    """Führt genau einen isolierten Colab-Job aus und liefert Downloadpfade."""
    source = resolve_input_video(uploaded_video, drive_path)
    engine = tts_engine.strip().casefold()
    speech_device = tts_device.strip().casefold()
    if engine not in TTS_ENGINES:
        raise ValueError(f"Unbekannte TTS-Engine: {tts_engine!r}")
    if speech_device not in TTS_DEVICES:
        raise ValueError(f"Unbekanntes TTS-Gerät: {tts_device!r}")

    reference_source = None
    piper_model_source = None
    piper_config_source = None
    if engine == "chatterbox":
        reference_source = resolve_optional_job_file(
            uploaded_tts_reference,
            tts_reference_drive_path,
            label="Stimmreferenz",
            allowed_extensions=AUDIO_EXTENSIONS,
            required=True,
        )
    if engine == "piper":
        piper_model_source = resolve_optional_job_file(
            uploaded_piper_model,
            piper_model_drive_path,
            label="Piper-Stimmenmodell",
            allowed_extensions=frozenset({".onnx"}),
            required=True,
        )
        piper_config_source = resolve_optional_job_file(
            uploaded_piper_config,
            piper_config_drive_path,
            label="Piper-Modellkonfiguration",
            allowed_extensions=frozenset({".json"}),
            required=True,
        )
    assets = {path for path in (reference_source, piper_model_source, piper_config_source) if path}
    required_space = (
        source.stat().st_size * 3 + sum(path.stat().st_size for path in assets) + 4 * 1024**3
    )
    free_space = shutil.disk_usage(COLAB_ROOT).free
    if free_space < required_space:
        raise ValueError(
            "Zu wenig freier Colab-Speicher für Video, Arbeitsdateien und Modell "
            f"({free_space / 2**30:.1f} GiB frei)."
        )
    job_id = uuid.uuid4().hex
    job_directory = JOB_ROOT / job_id
    output_directory = OUTPUT_ROOT / job_id
    keep_output = False
    pipeline: VideoDubbingPipeline | None = None
    active_cancel = cancel_event if cancel_event is not None else threading.Event()

    def report(update: ProgressUpdate) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, update.percent / 100)), update.message)

    try:
        check_cancelled(active_cancel)
        _cleanup_expired_directories(OUTPUT_ROOT)
        _cleanup_expired_directories(JOB_ROOT)
        job_directory.mkdir(parents=True, exist_ok=False)
        output_directory.mkdir(parents=True, exist_ok=False)
        local_source = job_directory / f"input{source.suffix.lower()}"
        _copy_file_cancellable(source, local_source, active_cancel)
        local_reference: Path | None = None
        if reference_source is not None:
            local_reference = job_directory / f"voice-reference{reference_source.suffix.lower()}"
            _copy_file_cancellable(reference_source, local_reference, active_cancel)
        local_piper_model: Path | None = None
        local_piper_config: Path | None = None
        if piper_model_source is not None:
            local_piper_model = job_directory / "piper-model.onnx"
            _copy_file_cancellable(piper_model_source, local_piper_model, active_cancel)
        if piper_config_source is not None:
            local_piper_config = job_directory / "piper-model.onnx.json"
            _copy_file_cancellable(piper_config_source, local_piper_config, active_cancel)
        check_cancelled(active_cancel)
        output_video = output_directory / (
            f"{_safe_filename(source.stem)}.translated-{target_language}.mp4"
        )
        config = AppConfig(
            target_language=target_language,
            source_language=None if source_language == "auto" else source_language,
            translation_service=translation_service,
            fallback_services=(
                ("google",)
                if translation_service.strip().casefold() == "gemini" and fallback_to_google
                else ()
            ),
            whisper_model=whisper_model,
            asr_engine=asr_engine,
            parakeet_model_directory=(
                Path(parakeet_model_directory.strip()) if parakeet_model_directory.strip() else None
            ),
            parakeet_profile=parakeet_profile,
            parakeet_threads=int(parakeet_threads),
            device=device,
            transcription_workers=1,
            nllb_device=nllb_device,
            tts_engine=engine,
            tts_device=speech_device,
            tts_voice=tts_voice or None,
            tts_reference_audio=local_reference,
            confirm_voice_rights=bool(confirm_voice_rights),
            piper_model=local_piper_model,
            piper_config=local_piper_config,
            piper_speaker_id=(None if piper_speaker_id is None else int(piper_speaker_id)),
            piper_length_scale=float(piper_length_scale),
            chatterbox_model=chatterbox_model,
            chatterbox_exaggeration=float(chatterbox_exaggeration),
            chatterbox_cfg_weight=float(chatterbox_cfg_weight),
            optimize_transcript=optimize_transcript,
            deepl_api_key=deepl_api_key or os.environ.get("DEEPL_API_KEY", ""),
            gemini_api_key=gemini_api_key or os.environ.get("GEMINI_API_KEY", ""),
            nllb_model=nllb_model,
            nllb_revision=nllb_revision or None,
            nllb_batch_size=int(nllb_batch_size),
            nllb_source_code=nllb_source_code or None,
            nllb_target_code=nllb_target_code or None,
            request_retries=int(request_retries),
            request_retry_base_seconds=float(request_retry_base_seconds),
            request_retry_max_seconds=max(30.0, float(request_retry_base_seconds)),
            gemini_request_pause_seconds=float(gemini_request_pause_seconds),
            overwrite=False,
        )
        pipeline = VideoDubbingPipeline(
            config,
            event_sink=report,
            cancel_event=active_cancel,
        )
        if pipeline_ready:
            pipeline_ready(pipeline)
        result = pipeline.run_path(local_source, output_video)
        completed = result.completed[0]
        artifacts = [
            completed.output_path,
            completed.original_subtitles,
            completed.translated_subtitles,
        ]
        drive_message = ""
        if save_to_drive:
            staging: Path | None = None
            try:
                drive_base = resolve_drive_output_directory(drive_output_folder)
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                drive_name = f"{_safe_filename(source.stem)}-{timestamp}-{job_id[:8]}"
                drive_job = drive_base / drive_name
                staging = drive_base / f".{drive_name}.{uuid.uuid4().hex}.partial"
                staging.mkdir(parents=True, exist_ok=False)
                for artifact in artifacts:
                    _copy_file_cancellable(
                        artifact,
                        staging / artifact.name,
                        active_cancel,
                    )
                staging.replace(drive_job)
                staging = None
                drive_message = f" Zusätzlich gespeichert in {drive_job}."
            except BaseException as exc:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                if not isinstance(exc, (OSError, ValueError)):
                    raise
                drive_message = (
                    f" Drive-Speicherung fehlgeschlagen; Browser-Downloads sind verfügbar: {exc}."
                )
        warnings = ""
        if completed.warnings:
            warnings = " Warnungen: " + " | ".join(completed.warnings)
        keep_output = True
        _schedule_output_cleanup(output_directory)
        return (
            f"Fertig mit {engine}: {completed.segment_count} Segmente, Quelle "
            f"{completed.source_language}.{drive_message}{warnings} "
            "Browser-Downloads bleiben sechs Stunden verfügbar.",
            [str(path) for path in artifacts],
        )
    except CancelledError:
        raise
    except (LinguoAIError, OSError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        if pipeline_ready and pipeline is not None:
            pipeline_ready(None)
        shutil.rmtree(job_directory, ignore_errors=True)
        if not keep_output:
            shutil.rmtree(output_directory, ignore_errors=True)


def _batch_sources(
    uploaded_videos: str | Sequence[str] | None,
    drive_paths: str,
) -> list[tuple[str | None, str, str]]:
    """Erzeugt eine geordnete Batch-Liste; Uploads haben Vorrang vor Drive-Pfaden."""
    if uploaded_videos:
        values = (
            [uploaded_videos]
            if isinstance(uploaded_videos, (str, os.PathLike))
            else list(uploaded_videos)
        )
        sources = []
        for value in values:
            path = str(value)
            sources.append((path, "", Path(path).name))
        if sources:
            return sources

    paths = [line.strip() for line in drive_paths.splitlines() if line.strip()]
    if not paths:
        raise ValueError("Lade mindestens ein Video hoch oder gib pro Zeile einen MyDrive-Pfad an.")
    return [(None, path, Path(path).name or path) for path in paths]


def process_colab_batch(
    uploaded_videos: str | Sequence[str] | None,
    drive_paths: str,
    target_language: str,
    source_language: str,
    translation_service: str,
    whisper_model: str,
    device: str,
    nllb_device: str,
    nllb_model: str,
    nllb_revision: str,
    nllb_batch_size: int,
    nllb_source_code: str,
    nllb_target_code: str,
    tts_voice: str,
    deepl_api_key: str,
    gemini_api_key: str,
    optimize_transcript: bool,
    save_to_drive: bool,
    drive_output_folder: str,
    *,
    asr_engine: str = "whisper",
    parakeet_model_directory: str = "",
    parakeet_profile: str = "multilingual-v3",
    parakeet_threads: int = 4,
    tts_engine: str = "edge",
    tts_device: str = "auto",
    uploaded_tts_reference: str | None = None,
    tts_reference_drive_path: str = "",
    confirm_voice_rights: bool = False,
    uploaded_piper_model: str | None = None,
    piper_model_drive_path: str = "",
    uploaded_piper_config: str | None = None,
    piper_config_drive_path: str = "",
    piper_speaker_id: int | float | None = None,
    piper_length_scale: float = 1.0,
    chatterbox_model: str = "v2",
    chatterbox_exaggeration: float = 0.5,
    chatterbox_cfg_weight: float = 0.5,
    fallback_to_google: bool = True,
    request_retries: int = 4,
    request_retry_base_seconds: float = 2.0,
    gemini_request_pause_seconds: float = 2.5,
    continue_after_error: bool = True,
    progress_callback: Callable[[float, str], None] | None = None,
    pipeline_ready: Callable[[VideoDubbingPipeline | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, list[str]]:
    """Verarbeitet mehrere Videos nacheinander und behält erfolgreiche Ergebnisse."""
    sources = _batch_sources(uploaded_videos, drive_paths)
    active_cancel = cancel_event if cancel_event is not None else threading.Event()
    artifacts: list[str] = []
    completed_messages: list[str] = []
    failed_messages: list[str] = []
    total = len(sources)

    for index, (uploaded_video, drive_path, label) in enumerate(sources):
        check_cancelled(active_cancel)
        if progress_callback:
            progress_callback(index / total, f"Batch {index + 1}/{total}: {label}")

        def report(value: float, message: str, *, position: int = index) -> None:
            if progress_callback:
                progress_callback(
                    min(1.0, (position + max(0.0, min(1.0, value))) / total),
                    f"Batch {position + 1}/{total}: {message}",
                )

        try:
            message, job_artifacts = process_colab_job(
                uploaded_video,
                drive_path,
                target_language,
                source_language,
                translation_service,
                whisper_model,
                device,
                nllb_device,
                nllb_model,
                nllb_revision,
                nllb_batch_size,
                nllb_source_code,
                nllb_target_code,
                tts_voice,
                deepl_api_key,
                gemini_api_key,
                optimize_transcript,
                save_to_drive,
                drive_output_folder,
                asr_engine=asr_engine,
                parakeet_model_directory=parakeet_model_directory,
                parakeet_profile=parakeet_profile,
                parakeet_threads=parakeet_threads,
                tts_engine=tts_engine,
                tts_device=tts_device,
                uploaded_tts_reference=uploaded_tts_reference,
                tts_reference_drive_path=tts_reference_drive_path,
                confirm_voice_rights=confirm_voice_rights,
                uploaded_piper_model=uploaded_piper_model,
                piper_model_drive_path=piper_model_drive_path,
                uploaded_piper_config=uploaded_piper_config,
                piper_config_drive_path=piper_config_drive_path,
                piper_speaker_id=piper_speaker_id,
                piper_length_scale=piper_length_scale,
                chatterbox_model=chatterbox_model,
                chatterbox_exaggeration=chatterbox_exaggeration,
                chatterbox_cfg_weight=chatterbox_cfg_weight,
                fallback_to_google=fallback_to_google,
                request_retries=request_retries,
                request_retry_base_seconds=request_retry_base_seconds,
                gemini_request_pause_seconds=gemini_request_pause_seconds,
                progress_callback=report,
                pipeline_ready=pipeline_ready,
                cancel_event=active_cancel,
            )
            artifacts.extend(job_artifacts)
            completed_messages.append(f"{label}: {message}")
        except CancelledError:
            raise
        except Exception as exc:
            failed_messages.append(f"{label}: {exc}")
            if not continue_after_error:
                raise

    if progress_callback:
        progress_callback(1.0, "Batch abgeschlossen.")
    if not completed_messages:
        raise RuntimeError("Kein Video konnte verarbeitet werden. " + " | ".join(failed_messages))

    summary = f"Batch fertig: {len(completed_messages)}/{total} Video(s) erfolgreich."
    if failed_messages:
        summary += " Fehler: " + " | ".join(failed_messages)
    return summary, artifacts


def build_demo(
    *,
    upload_limit_mb: int = DEFAULT_UPLOAD_LIMIT_MB,
    share_provider: str = "gradio",
) -> Any:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError(
            "Gradio fehlt. Installiere die Colab-Oberfläche mit 'pip install -e \".[colab]\"'."
        ) from exc

    language_choices = [(f"{name} ({code})", code) for code, name in LANGUAGES.items()]
    source_choices = [("Automatisch erkennen", "auto"), *language_choices]
    active_lock = threading.Lock()
    active_pipeline: list[VideoDubbingPipeline | None] = [None]
    ui_cancel_event = threading.Event()

    def set_active_pipeline(pipeline: VideoDubbingPipeline | None) -> None:
        with active_lock:
            active_pipeline[0] = pipeline

    with gr.Blocks(
        title="LinguoAI Colab",
        delete_cache=(60 * 60, OUTPUT_RETENTION_SECONDS),
    ) as demo:
        gr.Markdown(
            "# LinguoAI für Google Colab\n"
            "Mehrere Videos gemeinsam hochladen oder pro Zeile einen Pfad unter `MyDrive` "
            "angeben. Der Batch wird nacheinander verarbeitet, damit GPU-RAM und API-Limits "
            "nicht unnötig belastet werden. NLLB, Piper und Chatterbox rechnen lokal; "
            "Edge TTS und gTTS bleiben Online-Dienste."
        )
        upload_hint = (
            f"**Browser-Uploads: maximal {upload_limit_mb} MB pro Datei.** "
            "Große Videos, Stimmreferenzen und Modelle lassen sich auch über "
            "die MyDrive-Pfade verwenden. "
        )
        if share_provider == "cloudflare":
            upload_hint += (
                "Bei mehreren gleichzeitig ausgewählten Dateien gilt zusätzlich das "
                "Cloudflare-Limit für die gesamte Upload-Anfrage. "
                "Cloudflare Free/Pro erlaubt höchstens 100 MB pro Anfrage."
            )
        else:
            upload_hint += (
                "Der dynamische Gradio-Link benötigt weder eine eigene Domain "
                "noch einen Cloudflare-Zugang und gilt nur während dieser Sitzung."
            )
        gr.Markdown(upload_hint)
        with gr.Row():
            uploaded_video = gr.File(
                label="Videos direkt hochladen (mehrere gleichzeitig möglich)",
                file_types=["video"],
                file_count="multiple",
                type="filepath",
            )
            drive_path = gr.Textbox(
                label="Alternativ: MyDrive-Pfade, ein Video pro Zeile",
                placeholder="Videos/video-1.mp4\nVideos/video-2.mp4",
                lines=4,
            )
        with gr.Row():
            target_language = gr.Dropdown(
                language_choices,
                value="de",
                label="Zielsprache (BCP-47; eigener Code möglich)",
                allow_custom_value=True,
            )
            source_language = gr.Dropdown(
                source_choices,
                value="auto",
                label="Quellsprache",
            )
            translation_service = gr.Dropdown(
                [
                    ("NLLB lokal (kein API-Schlüssel)", "nllb"),
                    ("Google Translate", "google"),
                    ("DeepL", "deepl"),
                    ("Gemini", "gemini"),
                ],
                value="nllb",
                label="Übersetzung",
            )
        with gr.Row():
            whisper_model = gr.Dropdown(
                ["tiny", "base", "small", "medium", "large-v3"],
                value="small",
                label="Whisper-Modell",
            )
            device = gr.Dropdown(
                [("Automatisch (GPU, falls vorhanden)", "auto"), ("CPU", "cpu"), ("GPU", "cuda")],
                value="auto",
                label="Rechengerät",
            )
            nllb_device = gr.Dropdown(
                [("Automatisch", "auto"), ("CPU", "cpu"), ("GPU", "cuda")],
                value="auto",
                label="NLLB-Gerät",
            )
        with gr.Accordion("Lokales Modell und weitere Sprachen", open=False):
            gr.Markdown(
                "Das Standardmodell unterstützt viele Sprachen, steht aber unter "
                "**CC-BY-NC-4.0** und ist nicht für Produktion freigegeben. Ein eigenes "
                "NLLB-kompatibles Modell oder ein lokaler Modellpfad kann eingesetzt werden. "
                "Für eine zusätzliche Sprache hier auch deren BCP-47-Zielcode eintragen und "
                "eine passende Edge-TTS-Stimme angeben."
            )
            nllb_model = gr.Textbox(
                value="facebook/nllb-200-distilled-600M",
                label="Hugging-Face-Modell oder lokaler Pfad",
            )
            nllb_revision = gr.Textbox(
                label="Optionale feste Hub-Revision (Commit oder Tag)",
                placeholder="main oder Commit-Hash",
            )
            nllb_batch_size = gr.Slider(1, 32, value=8, step=1, label="NLLB-Batchgröße")
            with gr.Row():
                nllb_source_code = gr.Textbox(
                    label="Optionaler FLORES-200-Quellcode",
                    placeholder="deu_Latn",
                )
                nllb_target_code = gr.Textbox(
                    label="Optionaler FLORES-200-Zielcode",
                    placeholder="fra_Latn",
                )
        with gr.Accordion("Sprachausgabe (TTS) und eigene Stimme", open=False):
            gr.Markdown(
                "**Edge TTS** ist online und ohne lokales Modell startklar. **Piper** nutzt "
                "ein eigenes lokales ONNX-Stimmenmodell samt JSON-Konfiguration. **gTTS** "
                "nutzt Google Translate Text-to-Speech online und kann per Sprachcode "
                "gesteuert werden. "
                "**Chatterbox** erzeugt lokal eine Stimme aus einer Referenzaufnahme; das "
                "optionale Paket ist groß und sollte in der Installationszelle gewählt werden. "
                "Verwende nur Aufnahmen, für deren Stimme du eine ausdrückliche Erlaubnis hast. "
                "Upload-Dateien haben jeweils Vorrang vor Drive-Pfaden."
            )
            with gr.Row():
                tts_engine = gr.Dropdown(
                    [
                        ("Edge TTS (online)", "edge"),
                        ("gTTS / Google Translate TTS (online)", "gtts"),
                        ("Piper (lokales ONNX-Modell)", "piper"),
                        ("Chatterbox (lokale Stimmreferenz)", "chatterbox"),
                    ],
                    value="edge",
                    label="TTS-Engine",
                )
                tts_device = gr.Dropdown(
                    [("Automatisch", "auto"), ("CPU", "cpu"), ("GPU", "cuda")],
                    value="auto",
                    label="TTS-Gerät (nur Piper/Chatterbox)",
                )
                tts_voice = gr.Textbox(
                    label="Voice/Sprache (optional)",
                    placeholder="Edge: de-DE-KatjaNeural · gTTS: de",
                )
            gr.Markdown("#### Piper")
            with gr.Row():
                uploaded_piper_model = gr.File(
                    label="Piper-Modell hochladen (.onnx)",
                    file_types=[".onnx"],
                    type="filepath",
                )
                piper_model_drive_path = gr.Textbox(
                    label="Oder Piper-Modell in MyDrive",
                    placeholder="LinguoAI/voices/de_DE-voice.onnx",
                )
            with gr.Row():
                uploaded_piper_config = gr.File(
                    label="Piper-Konfiguration hochladen (.json)",
                    file_types=[".json"],
                    type="filepath",
                )
                piper_config_drive_path = gr.Textbox(
                    label="Oder Piper-Konfiguration in MyDrive",
                    placeholder="LinguoAI/voices/de_DE-voice.onnx.json",
                )
            with gr.Row():
                piper_speaker_id = gr.Number(
                    value=None,
                    minimum=0,
                    step=1,
                    precision=0,
                    label="Piper-Sprecher-ID (optional)",
                )
                piper_length_scale = gr.Slider(
                    0.25,
                    4.0,
                    value=1.0,
                    step=0.05,
                    label="Piper-Längenfaktor",
                )
            gr.Markdown("#### Chatterbox")
            with gr.Row():
                uploaded_tts_reference = gr.File(
                    label="Stimmreferenz hochladen (Audio)",
                    file_types=["audio"],
                    type="filepath",
                )
                tts_reference_drive_path = gr.Textbox(
                    label="Oder Stimmreferenz in MyDrive",
                    placeholder="LinguoAI/voices/referenz.wav",
                )
            confirm_voice_rights = gr.Checkbox(
                value=False,
                label=("Ich habe die Erlaubnis der betroffenen Person, diese Stimme zu verwenden."),
            )
            with gr.Row():
                chatterbox_model = gr.Dropdown(
                    [("V2 (stabile PyPI-Version)", "v2")],
                    value="v2",
                    label="Chatterbox-Modell",
                )
                chatterbox_exaggeration = gr.Slider(
                    0.0,
                    2.0,
                    value=0.5,
                    step=0.05,
                    label="Ausdrucksstärke",
                )
                chatterbox_cfg_weight = gr.Slider(
                    0.0,
                    1.0,
                    value=0.5,
                    step=0.05,
                    label="Prompt-Treue (CFG)",
                )
        with gr.Accordion("Spracherkennung: optionale Parakeet-CPU-Alternative", open=False):
            with gr.Row():
                asr_engine = gr.Dropdown(
                    choices=list(ASR_ENGINES), value="whisper", label="Spracherkennung"
                )
                parakeet_profile = gr.Dropdown(
                    choices=list(PARAKEET_PROFILES),
                    value="multilingual-v3",
                    label="Parakeet-Modellprofil",
                )
            parakeet_model_directory = gr.Textbox(
                label="Lokaler Parakeet-Modellordner auf dem Server / in Colab",
                placeholder="/content/models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
            )
            parakeet_threads = gr.Slider(1, 32, value=4, step=1, label="Parakeet-CPU-Threads")
            gr.Markdown(
                'Parakeet benötigt `pip install -e ".[parakeet]"` und einen vollständigen '
                "sherpa-onnx-Export (Encoder, Decoder, Joiner, tokens.txt und ggf. .onnx.data). "
                "Wähle die Quellsprache oben ausdrücklich aus. NVIDIA V3 unterstützt "
                "25 europäische Sprachen; das primeline-Profil ist für Deutsch. "
                "Whisper-Modell und ASR-GPU-Auswahl gelten für Whisper."
            )
        with gr.Accordion("Cloud-Schlüssel und Optimierung", open=False):
            deepl_api_key = gr.Textbox(label="DeepL API-Key", type="password")
            gemini_api_key = gr.Textbox(label="Gemini API-Key", type="password")
            optimize_transcript = gr.Checkbox(
                value=False,
                label="Transkript zusätzlich mit Gemini korrigieren",
            )
            fallback_to_google = gr.Checkbox(
                value=True,
                label="Bei Gemini-Ausfall automatisch Google Translate verwenden",
            )
            with gr.Row():
                request_retries = gr.Slider(1, 8, value=4, step=1, label="API-Versuche insgesamt")
                request_retry_base_seconds = gr.Slider(
                    0.5,
                    20.0,
                    value=2.0,
                    step=0.5,
                    label="Wartezeit nach Fehlern (Sekunden, exponentiell)",
                )
                gemini_request_pause_seconds = gr.Slider(
                    0.0,
                    20.0,
                    value=2.5,
                    step=0.5,
                    label="Mindestpause zwischen Gemini-Anfragen",
                )
            continue_after_error = gr.Checkbox(
                value=True,
                label="Batch nach einem fehlerhaften Video fortsetzen",
            )
        with gr.Row():
            save_to_drive = gr.Checkbox(value=False, label="Ergebnisse in MyDrive kopieren")
            drive_output_folder = gr.Textbox(
                value="LinguoAI Outputs",
                label="Relativer Ausgabeordner in MyDrive",
            )
        with gr.Row():
            run_button = gr.Button("Batch verarbeiten", variant="primary")
            cancel_button = gr.Button("Abbrechen", variant="stop")
        status = gr.Textbox(label="Status", interactive=False)
        downloads = gr.File(label="Ergebnisse herunterladen", file_count="multiple")

        inputs = [
            uploaded_video,
            drive_path,
            target_language,
            source_language,
            translation_service,
            whisper_model,
            device,
            nllb_device,
            nllb_model,
            nllb_revision,
            nllb_batch_size,
            nllb_source_code,
            nllb_target_code,
            tts_engine,
            tts_device,
            tts_voice,
            uploaded_tts_reference,
            tts_reference_drive_path,
            confirm_voice_rights,
            uploaded_piper_model,
            piper_model_drive_path,
            uploaded_piper_config,
            piper_config_drive_path,
            piper_speaker_id,
            piper_length_scale,
            chatterbox_model,
            chatterbox_exaggeration,
            chatterbox_cfg_weight,
            deepl_api_key,
            gemini_api_key,
            optimize_transcript,
            fallback_to_google,
            request_retries,
            request_retry_base_seconds,
            gemini_request_pause_seconds,
            continue_after_error,
            save_to_drive,
            drive_output_folder,
            asr_engine,
            parakeet_model_directory,
            parakeet_profile,
            parakeet_threads,
        ]

        progress_tracker = gr.Progress()
        private_event_options: dict[str, Any]
        if "api_visibility" in inspect.signature(run_button.click).parameters:
            private_event_options = {"api_visibility": "private"}
        else:
            # Gradio 5 verwendete hierfür noch api_name=False.
            private_event_options = {"api_name": False}

        def run_with_progress(
            uploaded_video_value: str | Sequence[str] | None,
            drive_path_value: str,
            target_language_value: str,
            source_language_value: str,
            translation_service_value: str,
            whisper_model_value: str,
            device_value: str,
            nllb_device_value: str,
            nllb_model_value: str,
            nllb_revision_value: str,
            nllb_batch_size_value: int,
            nllb_source_code_value: str,
            nllb_target_code_value: str,
            tts_engine_value: str,
            tts_device_value: str,
            tts_voice_value: str,
            uploaded_tts_reference_value: str | None,
            tts_reference_drive_path_value: str,
            confirm_voice_rights_value: bool,
            uploaded_piper_model_value: str | None,
            piper_model_drive_path_value: str,
            uploaded_piper_config_value: str | None,
            piper_config_drive_path_value: str,
            piper_speaker_id_value: int | float | None,
            piper_length_scale_value: float,
            chatterbox_model_value: str,
            chatterbox_exaggeration_value: float,
            chatterbox_cfg_weight_value: float,
            deepl_api_key_value: str,
            gemini_api_key_value: str,
            optimize_transcript_value: bool,
            fallback_to_google_value: bool,
            request_retries_value: int,
            request_retry_base_seconds_value: float,
            gemini_request_pause_seconds_value: float,
            continue_after_error_value: bool,
            save_to_drive_value: bool,
            drive_output_folder_value: str,
            asr_engine_value: str,
            parakeet_model_directory_value: str,
            parakeet_profile_value: str,
            parakeet_threads_value: int,
            progress: Any = progress_tracker,
        ) -> tuple[str, list[str]]:
            ui_cancel_event.clear()
            return process_colab_batch(
                uploaded_video_value,
                drive_path_value,
                target_language_value,
                source_language_value,
                translation_service_value,
                whisper_model_value,
                device_value,
                nllb_device_value,
                nllb_model_value,
                nllb_revision_value,
                nllb_batch_size_value,
                nllb_source_code_value,
                nllb_target_code_value,
                tts_voice_value,
                deepl_api_key_value,
                gemini_api_key_value,
                optimize_transcript_value,
                save_to_drive_value,
                drive_output_folder_value,
                asr_engine=asr_engine_value,
                parakeet_model_directory=parakeet_model_directory_value,
                parakeet_profile=parakeet_profile_value,
                parakeet_threads=parakeet_threads_value,
                tts_engine=tts_engine_value,
                tts_device=tts_device_value,
                uploaded_tts_reference=uploaded_tts_reference_value,
                tts_reference_drive_path=tts_reference_drive_path_value,
                confirm_voice_rights=confirm_voice_rights_value,
                uploaded_piper_model=uploaded_piper_model_value,
                piper_model_drive_path=piper_model_drive_path_value,
                uploaded_piper_config=uploaded_piper_config_value,
                piper_config_drive_path=piper_config_drive_path_value,
                piper_speaker_id=piper_speaker_id_value,
                piper_length_scale=piper_length_scale_value,
                chatterbox_model=chatterbox_model_value,
                chatterbox_exaggeration=chatterbox_exaggeration_value,
                chatterbox_cfg_weight=chatterbox_cfg_weight_value,
                fallback_to_google=fallback_to_google_value,
                request_retries=request_retries_value,
                request_retry_base_seconds=request_retry_base_seconds_value,
                gemini_request_pause_seconds=gemini_request_pause_seconds_value,
                continue_after_error=continue_after_error_value,
                progress_callback=lambda value, message: progress(value, desc=message),
                pipeline_ready=set_active_pipeline,
                cancel_event=ui_cancel_event,
            )

        run_event = run_button.click(
            run_with_progress,
            inputs=inputs,
            outputs=[status, downloads],
            **private_event_options,
        )

        def cancel_active_job() -> str:
            ui_cancel_event.set()
            with active_lock:
                pipeline = active_pipeline[0]
            if pipeline is None:
                return "Abbruch angefordert …"
            pipeline.cancel()
            return "Abbruch angefordert …"

        cancel_button.click(
            cancel_active_job,
            outputs=status,
            cancels=[run_event],
            queue=False,
            **private_event_options,
        )
    return demo.queue(default_concurrency_limit=1, max_size=2)


def _validated_tunnel_hostname(value: str) -> str:
    """Prüft die feste öffentliche Adresse eines Cloudflare-Tunnels."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise ValueError(
            "Die Cloudflare-Adresse muss eine vollständige HTTPS-Adresse sein, "
            "zum Beispiel https://app.linguoai.de."
        ) from error

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Die Cloudflare-Adresse muss eine vollständige HTTPS-Adresse ohne "
            "Port, Pfad oder Parameter sein, zum Beispiel https://app.linguoai.de."
        )
    return f"https://{parsed.hostname}"


def _resolve_cloudflared_command(command: str) -> str:
    """Findet cloudflared oder gibt eine umsetzbare Colab-Fehlermeldung aus."""
    command = command.strip()
    resolved = shutil.which(command) if command else None
    if resolved:
        return resolved
    candidate = Path(command).expanduser() if command else None
    if candidate and candidate.is_file():
        return str(candidate)
    raise RuntimeError(
        "cloudflared wurde nicht gefunden. Führe zuerst die "
        "Startzelle im Notebook mit SHARE_PROVIDER = 'cloudflare' aus."
    )


def _stop_active_tunnel() -> None:
    """Beendet den Tunnelprozess beim Neustart oder Ende der Colab-Laufzeit."""
    global _active_tunnel_process
    process = _active_tunnel_process
    _active_tunnel_process = None
    if process is None or process.poll() is not None:
        return
    with suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=3)
    except OSError:
        pass


def _wait_for_cloudflare_tunnel(
    process: subprocess.Popen[str],
    readiness_url: str,
    *,
    timeout: float,
) -> None:
    """Wartet auf eine Edge-Verbindung; /ready bestätigt nicht die Hostname-Route."""
    # Lokale Statusabfragen dürfen nicht an einen HTTP-Proxy weitergegeben werden.
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError(
                "Cloudflare Tunnel wurde vor dem Verbindungsaufbau beendet. "
                "Prüfe den Tunnel-Token und die Cloudflare-Konfiguration."
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Cloudflare Tunnel hat innerhalb der Startfrist keine Verbindung aufgebaut. "
                "Prüfe Tunnel-Token und Netzwerkzugriff zu Cloudflare."
            )
        try:
            with opener.open(readiness_url, timeout=min(1.0, remaining)) as response:
                if response.status == 200:
                    return
        except OSError:
            # Verbindungsfehler und HTTP 503 sind während des Starts normal.
            pass
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _start_cloudflare_tunnel(
    token: str,
    hostname: str,
    *,
    command: str = "cloudflared",
    startup_timeout: float = DEFAULT_TUNNEL_STARTUP_TIMEOUT,
) -> str:
    """Startet einen namensgebundenen Cloudflare Tunnel ohne Token im Prozessaufruf."""
    global _active_tunnel_process
    if not math.isfinite(startup_timeout) or startup_timeout <= 0:
        raise ValueError("Die Tunnel-Startfrist muss eine positive endliche Zahl sein.")
    token = token.strip()
    if not token:
        raise RuntimeError(
            "CLOUDFLARE_TUNNEL_TOKEN fehlt. Lege ihn als Colab Secret an oder "
            "gib ihn verdeckt beim Start ein."
        )
    public_url = _validated_tunnel_hostname(hostname)
    cloudflared = _resolve_cloudflared_command(command)
    _stop_active_tunnel()
    environment = os.environ.copy()
    environment["TUNNEL_TOKEN"] = token
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as metrics_socket:
        metrics_socket.bind(("127.0.0.1", 0))
        metrics_port = metrics_socket.getsockname()[1]
    metrics_address = f"127.0.0.1:{metrics_port}"
    try:
        _active_tunnel_process = subprocess.Popen(
            [cloudflared, "tunnel", "--metrics", metrics_address, "run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            text=True,
        )
        _wait_for_cloudflare_tunnel(
            _active_tunnel_process,
            f"http://{metrics_address}/ready",
            timeout=startup_timeout,
        )
    except BaseException as error:
        _stop_active_tunnel()
        if isinstance(error, OSError):
            raise RuntimeError("Cloudflare Tunnel konnte nicht gestartet werden.") from error
        raise
    return public_url


atexit.register(_stop_active_tunnel)


def launch_colab(
    *,
    share_provider: str = "gradio",
    tunnel_token: str | None = None,
    tunnel_hostname: str | None = None,
    cloudflared_command: str = "cloudflared",
    server_port: int = DEFAULT_GRADIO_PORT,
    upload_limit_mb: int | None = None,
    tunnel_startup_timeout: float = DEFAULT_TUNNEL_STARTUP_TIMEOUT,
) -> None:
    """Startet einen Gradio-Link oder optional einen eigenen Cloudflare Tunnel."""
    provider = share_provider.strip().casefold()
    if provider not in {"gradio", "cloudflare"}:
        raise ValueError("SHARE_PROVIDER muss 'gradio' oder 'cloudflare' sein.")
    if upload_limit_mb is None:
        upload_limit_mb = (
            DEFAULT_CLOUDFLARE_UPLOAD_LIMIT_MB
            if provider == "cloudflare"
            else DEFAULT_UPLOAD_LIMIT_MB
        )
    if (
        not isinstance(upload_limit_mb, int)
        or isinstance(upload_limit_mb, bool)
        or upload_limit_mb <= 0
    ):
        raise ValueError("Das Upload-Limit muss eine positive ganze Zahl in MB sein.")
    if (
        not isinstance(server_port, int)
        or isinstance(server_port, bool)
        or not 0 < server_port < 65536
    ):
        raise ValueError("Der lokale Server-Port muss zwischen 1 und 65535 liegen.")
    token = ""
    public_url = ""
    if provider == "cloudflare":
        if not math.isfinite(tunnel_startup_timeout) or tunnel_startup_timeout <= 0:
            raise ValueError("Die Tunnel-Startfrist muss eine positive endliche Zahl sein.")
        token = tunnel_token or os.environ.get(CLOUDFLARE_TUNNEL_TOKEN_ENV, "")
        hostname = tunnel_hostname or os.environ.get(CLOUDFLARE_TUNNEL_HOSTNAME_ENV, "")
        public_url = _validated_tunnel_hostname(hostname)
        if not token.strip():
            raise RuntimeError(
                "CLOUDFLARE_TUNNEL_TOKEN fehlt. Hinterlege den Token als Colab Secret "
                "oder gib ihn in der Startzelle verdeckt ein."
            )
    _stop_active_tunnel()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_expired_directories(OUTPUT_ROOT)
    _cleanup_expired_directories(JOB_ROOT)
    username = "linguoai"
    password = secrets.token_urlsafe(12)
    print(f"LinguoAI-Anmeldung: Benutzer={username}  Passwort={password}")
    demo = build_demo(upload_limit_mb=upload_limit_mb, share_provider=provider)
    try:
        demo.launch(
            share=provider == "gradio",
            server_name="127.0.0.1",
            server_port=server_port,
            prevent_thread_lock=True,
            inline=False,
            auth=(username, password),
            allowed_paths=[str(OUTPUT_ROOT)],
            blocked_paths=[str(DRIVE_ROOT)],
            max_file_size=upload_limit_mb * 1_000_000,
            show_error=True,
        )
        if provider == "cloudflare":
            public_url = _start_cloudflare_tunnel(
                token,
                public_url,
                command=cloudflared_command,
                startup_timeout=tunnel_startup_timeout,
            )
            print(f"Cloudflare verbunden. Konfigurierte Webadresse: {public_url}")
            print(
                f"Der öffentliche Hostname muss im Cloudflare-Dashboard auf "
                f"http://127.0.0.1:{server_port} zeigen."
            )
        else:
            public_url = demo.share_url
            if not public_url:
                raise RuntimeError(
                    "Gradio konnte keinen öffentlichen Link erstellen. Prüfe den "
                    "Netzwerkzugriff und starte die Notebook-Zelle erneut."
                )
            print(f"Dynamischer Gradio-Link: {public_url}")
        print("Die Adresse ist erreichbar, solange diese Colab-Sitzung und die Freigabe laufen.")
        demo.block_thread()
    finally:
        _stop_active_tunnel()
        with suppress(Exception):
            demo.close()


def entrypoint() -> NoReturn:
    launch_colab()
    raise SystemExit(0)


def _safe_filename(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "-" for character in value
    )
    return cleaned.strip("-.")[:80] or "video"


def _cleanup_expired_directories(
    root: Path,
    *,
    max_age_seconds: int = OUTPUT_RETENTION_SECONDS,
) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for candidate in root.iterdir():
        if not candidate.is_dir() or len(candidate.name) != 32:
            continue
        try:
            int(candidate.name, 16)
            if candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
        except (OSError, ValueError):
            continue


def _schedule_output_cleanup(output_directory: Path) -> None:
    timer = threading.Timer(
        OUTPUT_RETENTION_SECONDS,
        shutil.rmtree,
        args=(output_directory,),
        kwargs={"ignore_errors": True},
    )
    timer.daemon = True
    timer.start()


def _copy_file_cancellable(
    source: Path,
    destination: Path,
    cancel_event: threading.Event,
    *,
    chunk_size: int = 8 * 1024 * 1024,
) -> None:
    """Kopiert große Colab-/Drive-Dateien mit Abbruchprüfpunkten."""
    try:
        with source.open("rb") as source_handle, destination.open("xb") as target_handle:
            while True:
                check_cancelled(cancel_event)
                chunk = source_handle.read(chunk_size)
                if not chunk:
                    break
                target_handle.write(chunk)
        check_cancelled(cancel_event)
        shutil.copystat(source, destination)
    except BaseException:
        with suppress(OSError):
            destination.unlink(missing_ok=True)
        raise
