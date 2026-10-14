"""CLI-Controller mit korrekten Exitcodes; ohne eager GUI-/ML-Imports."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from . import __version__
from .config import (
    ASR_ENGINES,
    PARAKEET_PROFILES,
    TRANSLATION_SERVICES,
    TTS_DEVICES,
    TTS_ENGINES,
    AppConfig,
)
from .domain import ProgressUpdate
from .errors import CancelledError, LinguoAIError
from .pipeline import VideoDubbingPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linguoai",
        description="Transkribiert, übersetzt und synchronisiert die Tonspur von Videos.",
    )
    parser.add_argument("-i", "--input", help="Videodatei oder Ordner")
    parser.add_argument("-o", "--output", help="Zieldatei oder Zielordner")
    parser.add_argument("-l", "--language", default="en", help="Zielsprache, z. B. de oder en")
    parser.add_argument(
        "--source-language", help="Quellsprache: Whisper automatisch, bei Parakeet erforderlich"
    )
    parser.add_argument(
        "-s",
        "--service",
        default="google",
        choices=TRANSLATION_SERVICES,
        help="Exklusiv verwendeter primärer Übersetzungsdienst",
    )
    parser.add_argument(
        "--fallback-service",
        action="append",
        default=[],
        choices=TRANSLATION_SERVICES,
        help="Expliziter Cloud-Fallback; Option darf mehrfach vorkommen",
    )
    parser.add_argument("--whisper-model", default="medium", help="z. B. small, medium, large-v3")
    parser.add_argument("--asr-engine", choices=ASR_ENGINES, default="whisper")
    parser.add_argument(
        "--parakeet-model-directory",
        type=Path,
        help="Lokaler sherpa-onnx-Export mit encoder, decoder, joiner und tokens.txt",
    )
    parser.add_argument("--parakeet-profile", choices=PARAKEET_PROFILES, default="multilingual-v3")
    parser.add_argument("--parakeet-threads", type=int, default=4, help="Parakeet-CPU-Threads")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--compute-type", default="default", help="z. B. int8 oder float16")
    parser.add_argument("--transcription-workers", type=int, default=2)
    parser.add_argument("--tts-workers", type=int, default=4)
    parser.add_argument(
        "--tts-engine",
        choices=TTS_ENGINES,
        default="edge",
        help="Sprachausgabe: Edge/gTTS online, Piper lokal oder Chatterbox-Stimmreferenz",
    )
    parser.add_argument(
        "--tts-device",
        choices=TTS_DEVICES,
        default="auto",
        help="Rechengerät für lokale TTS-Engines",
    )
    parser.add_argument(
        "--tts-voice",
        help=(
            "Optionale Stimme/Sprache: Edge-Stimmen-ID, z. B. de-DE-KatjaNeural; "
            "bei gTTS Sprachcode, z. B. de oder en"
        ),
    )
    parser.add_argument(
        "--tts-reference-audio",
        type=Path,
        help="Referenzaufnahme für Chatterbox (nur mit Nutzungsrecht)",
    )
    parser.add_argument(
        "--confirm-voice-rights",
        action="store_true",
        help="Bestätigt die Erlaubnis zur Nutzung der Chatterbox-Referenzstimme",
    )
    parser.add_argument("--piper-model", type=Path, help="Lokales Piper-Modell im ONNX-Format")
    parser.add_argument(
        "--piper-config",
        type=Path,
        help="Piper-Modellkonfiguration (.onnx.json oder .json)",
    )
    parser.add_argument("--piper-speaker-id", type=int, help="Optionale Sprecher-ID des Modells")
    parser.add_argument(
        "--piper-length-scale",
        type=float,
        default=1.0,
        help="Piper-Sprechtempo als Längenfaktor (0.25 bis 4.0)",
    )
    parser.add_argument(
        "--chatterbox-model",
        choices=("v2",),
        default="v2",
        help="Veröffentlichtes Chatterbox-Multilingual-Modell (derzeit v2)",
    )
    parser.add_argument(
        "--chatterbox-exaggeration",
        type=float,
        default=0.5,
        help="Ausdrucksstärke für Chatterbox (0 bis 2)",
    )
    parser.add_argument(
        "--chatterbox-cfg-weight",
        type=float,
        default=0.5,
        help="Prompt-Treue für Chatterbox (0 bis 1)",
    )
    parser.add_argument("--chunk-seconds", type=float, default=120.0)
    parser.add_argument("--chunk-overlap", type=float, default=1.25)
    parser.add_argument("--max-tts-speed", type=float, default=2.2)
    parser.add_argument(
        "--preferred-tts-speed",
        type=float,
        default=1.35,
        help="Zieltempo für adaptive Synchronisierung vor dem Notfall-Limit",
    )
    parser.add_argument(
        "--no-adaptive-timing",
        action="store_false",
        dest="adaptive_timing",
        help="Intelligente Nutzung von Pausen, Nachbarfenstern und Cue-Bündelung deaktivieren",
    )
    parser.add_argument(
        "--no-auto-shorten",
        action="store_false",
        dest="automatic_translation_shortening",
        help="Gemini-Kürzung zu langer Zieltext-Cues deaktivieren",
    )
    parser.add_argument(
        "--no-context-aware-translation",
        action="store_false",
        dest="context_aware_translation",
        help="Gesamttranskript nicht als Übersetzungskontext verwenden",
    )
    parser.add_argument("--translation-context-characters", type=int, default=100_000)
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="ASR-Text cue-stabil mit Gemini korrigieren",
    )
    parser.add_argument("--gemini-model", default="gemini-3.5-flash")
    parser.add_argument(
        "--nllb-model",
        default="facebook/nllb-200-distilled-600M",
        help="Hugging-Face-ID oder lokaler Pfad eines NLLB-kompatiblen Modells",
    )
    parser.add_argument("--nllb-device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--nllb-batch-size", type=int, default=8)
    parser.add_argument("--nllb-max-input-tokens", type=int, default=512)
    parser.add_argument("--nllb-max-new-tokens", type=int, default=256)
    parser.add_argument(
        "--nllb-source-code",
        help="Optionaler FLORES-200-Quellcode, z. B. deu_Latn",
    )
    parser.add_argument(
        "--nllb-target-code",
        help="Optionaler FLORES-200-Zielcode für weitere NLLB-Sprachen",
    )
    parser.add_argument(
        "--nllb-revision",
        help="Optionale feste Hub-Revision (Commit/Tag) für reproduzierbares Laden",
    )
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument(
        "--request-retries",
        type=int,
        default=4,
        help="Gesamtzahl der Provider-Versuche einschließlich Erstaufruf",
    )
    parser.add_argument("--temp-directory", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--gui", action="store_true", help="Grafische Oberfläche öffnen")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return _launch_gui()

    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.gui:
        return _launch_gui()
    if not args.input or not args.output:
        parser.error("--input und --output sind im CLI-Modus erforderlich")

    config = AppConfig(
        target_language=args.language,
        source_language=args.source_language,
        translation_service=args.service,
        fallback_services=tuple(args.fallback_service),
        whisper_model=args.whisper_model,
        asr_engine=args.asr_engine,
        parakeet_model_directory=args.parakeet_model_directory,
        parakeet_profile=args.parakeet_profile,
        parakeet_threads=args.parakeet_threads,
        device=args.device,
        compute_type=args.compute_type,
        transcription_workers=args.transcription_workers,
        tts_workers=args.tts_workers,
        tts_engine=args.tts_engine,
        tts_device=args.tts_device,
        tts_voice=args.tts_voice,
        tts_reference_audio=args.tts_reference_audio,
        confirm_voice_rights=args.confirm_voice_rights,
        piper_model=args.piper_model,
        piper_config=args.piper_config,
        piper_speaker_id=args.piper_speaker_id,
        piper_length_scale=args.piper_length_scale,
        chatterbox_model=args.chatterbox_model,
        chatterbox_exaggeration=args.chatterbox_exaggeration,
        chatterbox_cfg_weight=args.chatterbox_cfg_weight,
        chunk_seconds=args.chunk_seconds,
        chunk_overlap_seconds=args.chunk_overlap,
        max_tts_speed=args.max_tts_speed,
        preferred_tts_speed=args.preferred_tts_speed,
        adaptive_timing=args.adaptive_timing,
        automatic_translation_shortening=args.automatic_translation_shortening,
        context_aware_translation=args.context_aware_translation,
        translation_context_characters=args.translation_context_characters,
        optimize_transcript=args.optimize,
        gemini_model=args.gemini_model,
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        deepl_api_key=os.environ.get("DEEPL_API_KEY", ""),
        nllb_model=args.nllb_model,
        nllb_device=args.nllb_device,
        nllb_batch_size=args.nllb_batch_size,
        nllb_max_input_tokens=args.nllb_max_input_tokens,
        nllb_max_new_tokens=args.nllb_max_new_tokens,
        nllb_source_code=args.nllb_source_code,
        nllb_target_code=args.nllb_target_code,
        nllb_revision=args.nllb_revision,
        request_timeout_seconds=args.request_timeout,
        request_retries=args.request_retries,
        overwrite=args.overwrite,
        temp_directory=args.temp_directory,
    )

    def report(update: ProgressUpdate) -> None:
        if not args.quiet:
            prefix = f"[{update.percent:6.2f}%]"
            if update.file_count > 1:
                prefix += f" [{update.file_index}/{update.file_count}]"
            print(f"{prefix} {update.message}", flush=True)

    pipeline: VideoDubbingPipeline | None = None
    try:
        pipeline = VideoDubbingPipeline(config, event_sink=report)
        outcome = pipeline.run_path(args.input, args.output)
    except KeyboardInterrupt:
        if pipeline:
            pipeline.cancel()
        print("Abgebrochen.", file=sys.stderr)
        return 130
    except CancelledError as exc:
        print(str(exc), file=sys.stderr)
        return 130
    except LinguoAIError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Unerwarteter Fehler: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 3

    for result in outcome.completed:
        print(f"Erstellt: {result.output_path}")
        print(f"Untertitel: {result.original_subtitles}")
        print(f"Untertitel: {result.translated_subtitles}")
        for warning in result.warnings:
            print(f"Warnung: {warning}", file=sys.stderr)
    for failure in outcome.failed:
        print(f"Fehlgeschlagen: {failure.input_path}: {failure.error}", file=sys.stderr)
    return 1 if outcome.failed else 0


def _launch_gui() -> int:
    try:
        from .gui import run_gui
    except ImportError as exc:
        print(
            "GUI-Abhängigkeit fehlt. Installiere sie mit 'pip install -e \".[gui]\"'. "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 2
    run_gui()
    return 0


def entrypoint() -> NoReturn:
    raise SystemExit(main())


def gui_entrypoint() -> NoReturn:
    raise SystemExit(_launch_gui())
