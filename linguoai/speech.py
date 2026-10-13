"""TLS-validierte, abbrechbare Online- und lokale TTS-Spracherzeugung."""

from __future__ import annotations

import asyncio
import gc
import math
import threading
import time
import uuid
import wave
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any, ClassVar, Protocol

from .config import AppConfig
from .domain import Segment
from .errors import CancelledError, DependencyError, SpeechSynthesisError
from .runtime import check_cancelled, retry_call

DEFAULT_EDGE_VOICES: dict[str, str] = {
    "ar": "ar-SA-ZariyahNeural",
    "bg": "bg-BG-KalinaNeural",
    "cs": "cs-CZ-VlastaNeural",
    "da": "da-DK-ChristelNeural",
    "de": "de-DE-KatjaNeural",
    "el": "el-GR-AthinaNeural",
    "en": "en-US-JennyNeural",
    "es": "es-ES-ElviraNeural",
    "fi": "fi-FI-NooraNeural",
    "fr": "fr-FR-DeniseNeural",
    "he": "he-IL-HilaNeural",
    "hi": "hi-IN-SwaraNeural",
    "hu": "hu-HU-NoemiNeural",
    "id": "id-ID-GadisNeural",
    "it": "it-IT-ElsaNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "ms": "ms-MY-YasminNeural",
    "nl": "nl-NL-ColetteNeural",
    "no": "nb-NO-PernilleNeural",
    "pl": "pl-PL-ZofiaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ro": "ro-RO-AlinaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "sv": "sv-SE-SofieNeural",
    "th": "th-TH-PremwadeeNeural",
    "tl": "fil-PH-BlessicaNeural",
    "tr": "tr-TR-EmelNeural",
    "uk": "uk-UA-PolinaNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "zh-CN": "zh-CN-XiaoxiaoNeural",
}


class SpeechSynthesizer(Protocol):
    """Gemeinsamer Vertrag aller Sprach-Engines."""

    def preflight(self) -> None: ...

    def synthesize(
        self,
        segments: Sequence[Segment],
        output_directory: Path,
        *,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Path]: ...


class _CombinedEvent(threading.Event):
    """Threading-Event-ähnliche Sicht auf Nutzerabbruch und internen Sofortabbruch."""

    def __init__(self, *events: threading.Event) -> None:
        super().__init__()
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._events[0].wait(0.05 if remaining is None else min(0.05, remaining))
        return True


class EdgeTTSSynthesizer:
    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or (lambda _message: None)

    def preflight(self) -> None:
        self._load_module()
        self._voice()

    def synthesize(
        self,
        segments: Sequence[Segment],
        output_directory: Path,
        *,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        if not segments:
            raise SpeechSynthesisError("Keine Segmente für TTS vorhanden.")
        edge_tts = self._load_module()
        voice = self._voice()
        output_directory.mkdir(parents=True, exist_ok=True)
        outputs = [output_directory / f"voice-{index:05d}.mp3" for index in range(len(segments))]
        completed = 0
        abort_event = threading.Event()
        combined_event = _CombinedEvent(cancel_event, abort_event)

        with ThreadPoolExecutor(
            max_workers=min(self.config.tts_workers, len(segments)),
            thread_name_prefix="edge-tts",
        ) as executor:
            futures = {
                executor.submit(
                    self._synthesize_one,
                    edge_tts,
                    segment.text,
                    voice,
                    outputs[index],
                    combined_event,
                ): index
                for index, segment in enumerate(segments)
            }
            try:
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        future.result()
                    except CancelledError:
                        raise
                    except Exception as exc:
                        raise SpeechSynthesisError(
                            f"TTS für Segment {index + 1} ist fehlgeschlagen: {exc}"
                        ) from exc
                    completed += 1
                    if on_progress:
                        on_progress(completed, len(segments))
            except BaseException:
                # Bereits laufende Futures lassen sich nicht über Future.cancel()
                # stoppen. Das gemeinsame Event beendet ihre async Saves/Retry-Wartezeiten.
                abort_event.set()
                for pending in futures:
                    pending.cancel()
                raise
        return outputs

    def _synthesize_one(
        self,
        edge_tts: object,
        text: str,
        voice: str,
        output: Path,
        cancel_event: threading.Event,
    ) -> None:
        check_cancelled(cancel_event)
        temporary_paths: list[Path] = []
        successful_path: list[Path] = []
        timeout = max(1, math.ceil(self.config.request_timeout_seconds))

        def create() -> None:
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial.mp3")
            temporary_paths.append(temporary)

            async def save() -> None:
                communicate = edge_tts.Communicate(  # type: ignore[attr-defined]
                    text,
                    voice,
                    connect_timeout=min(15, timeout),
                    receive_timeout=timeout,
                )
                task = asyncio.create_task(communicate.save(str(temporary)))
                try:
                    while not task.done():
                        if cancel_event.is_set():
                            task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
                            raise CancelledError("Verarbeitung wurde abgebrochen.")
                        await asyncio.wait({task}, timeout=0.2)
                    await task
                finally:
                    if not task.done():
                        task.cancel()

            asyncio.run(save())
            if not temporary.is_file() or temporary.stat().st_size < 128:
                raise SpeechSynthesisError("Edge TTS lieferte keine gültige Audiodatei.")
            successful_path[:] = [temporary]

        try:
            retry_call(
                create,
                attempts=self.config.request_retries,
                cancel_event=cancel_event,
                on_retry=lambda attempt, exc: self.logger(
                    f"Edge TTS: Retry {attempt} nach Fehler: {exc}"
                ),
                should_retry=self._is_retryable,
            )
            if not successful_path:
                raise SpeechSynthesisError("Edge TTS erzeugte keine Ausgabedatei.")
            check_cancelled(cancel_event)
            successful_path[0].replace(output)
        finally:
            for temporary in temporary_paths:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.logger(f"Temporäre TTS-Datei blieb zurück: {temporary.name}")

    def _voice(self) -> str:
        if self.config.tts_voice:
            return self.config.tts_voice
        try:
            return DEFAULT_EDGE_VOICES[self.config.target_language]
        except KeyError as exc:
            raise SpeechSynthesisError(
                f"Keine Standardstimme für {self.config.target_language!r}. "
                "Gib über --tts-voice eine Edge-TTS-Stimme an."
            ) from exc

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        return not isinstance(error, (ValueError, TypeError, DependencyError))

    @staticmethod
    def _load_module() -> object:
        try:
            import edge_tts
        except ImportError as exc:
            raise DependencyError(
                "edge-tts fehlt. Installiere das Projekt mit 'pip install -e .'."
            ) from exc
        return edge_tts


class GoogleTTSSynthesizer:
    """Online-Sprachausgabe mit gTTS / Google Translate Text-to-Speech."""

    _LANGUAGE_ALIASES: ClassVar[dict[str, str]] = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "zh-hans": "zh-CN",
        "he": "iw",
    }

    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or (lambda _message: None)

    def preflight(self) -> None:
        self._load_module()
        self._language()

    def synthesize(
        self,
        segments: Sequence[Segment],
        output_directory: Path,
        *,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        if not segments:
            raise SpeechSynthesisError("Keine Segmente fÃ¼r TTS vorhanden.")
        gtts_class = self._load_module()
        language = self._language()
        output_directory.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        for index, segment in enumerate(segments, start=1):
            check_cancelled(cancel_event)
            output = output_directory / f"voice-{index - 1:05d}.mp3"
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial.mp3")
            try:
                self._synthesize_one(gtts_class, segment.text, language, temporary, cancel_event)
                check_cancelled(cancel_event)
                temporary.replace(output)
            except CancelledError:
                raise
            except Exception as exc:
                raise SpeechSynthesisError(
                    f"gTTS fÃ¼r Segment {index} ist fehlgeschlagen: {exc}"
                ) from exc
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            outputs.append(output)
            if on_progress:
                on_progress(index, len(segments))
        return outputs

    def _synthesize_one(
        self,
        gtts_class: Any,
        text: str,
        language: str,
        output: Path,
        cancel_event: threading.Event,
    ) -> None:
        def create() -> None:
            check_cancelled(cancel_event)
            tts = gtts_class(text=text, lang=language)
            tts.save(str(output))
            if not output.is_file() or output.stat().st_size < 128:
                raise SpeechSynthesisError("gTTS lieferte keine gÃ¼ltige Audiodatei.")

        retry_call(
            create,
            attempts=self.config.request_retries,
            cancel_event=cancel_event,
            on_retry=lambda attempt, exc: self.logger(f"gTTS: Retry {attempt} nach Fehler: {exc}"),
            should_retry=self._is_retryable,
        )

    def _language(self) -> str:
        language = (self.config.tts_voice or self.config.target_language).strip()
        key = language.casefold()
        if key in self._LANGUAGE_ALIASES:
            return self._LANGUAGE_ALIASES[key]
        if "-" in language:
            base, region = language.split("-", maxsplit=1)
            if len(region) == 2:
                return base.casefold()
        return language

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        return not isinstance(error, (ValueError, TypeError, DependencyError))

    @staticmethod
    def _load_module() -> Any:
        try:
            from gtts import gTTS
        except ImportError as exc:
            raise DependencyError("gTTS fehlt. Installiere es mit: pip install gTTS") from exc
        return gTTS


class PiperTTSSynthesizer:
    """Lokale Piper-Stimme aus einem ONNX-Modell."""

    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or (lambda _message: None)
        self._voice: Any | None = None
        self._piper: Any | None = None
        self._using_cuda = False

    def preflight(self) -> None:
        self._piper = self._load_module()
        self._using_cuda = self._resolve_cuda()

    def synthesize(
        self,
        segments: Sequence[Segment],
        output_directory: Path,
        *,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        if not segments:
            raise SpeechSynthesisError("Keine Segmente für TTS vorhanden.")
        check_cancelled(cancel_event)
        try:
            voice, piper = self._load_voice()
        except (DependencyError, SpeechSynthesisError):
            raise
        except Exception as exc:
            raise SpeechSynthesisError(f"Piper-Stimme konnte nicht geladen werden: {exc}") from exc
        output_directory.mkdir(parents=True, exist_ok=True)
        syn_config = piper.SynthesisConfig(
            speaker_id=self.config.piper_speaker_id,
            length_scale=self.config.piper_length_scale,
        )
        outputs: list[Path] = []
        for index, segment in enumerate(segments, start=1):
            check_cancelled(cancel_event)
            output = output_directory / f"voice-{index - 1:05d}.wav"
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial.wav")
            try:
                self._synthesize_one(
                    voice,
                    segment.text,
                    temporary,
                    syn_config,
                    cancel_event,
                )
                check_cancelled(cancel_event)
                temporary.replace(output)
            except CancelledError:
                raise
            except Exception as exc:
                raise SpeechSynthesisError(
                    f"Piper-TTS für Segment {index} ist fehlgeschlagen: {exc}"
                ) from exc
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            outputs.append(output)
            if on_progress:
                on_progress(index, len(segments))
        return outputs

    @staticmethod
    def _synthesize_one(
        voice: Any,
        text: str,
        output: Path,
        syn_config: Any,
        cancel_event: threading.Event,
    ) -> None:
        wrote_audio = False
        wav_file: wave.Wave_write | None = None
        with ExitStack() as stack:
            for chunk in voice.synthesize(text, syn_config=syn_config):
                check_cancelled(cancel_event)
                if wav_file is None:
                    wav_file = stack.enter_context(wave.open(str(output), "wb"))
                    wav_file.setframerate(chunk.sample_rate)
                    wav_file.setsampwidth(chunk.sample_width)
                    wav_file.setnchannels(chunk.sample_channels)
                wav_file.writeframes(chunk.audio_int16_bytes)
                wrote_audio = True
        if not wrote_audio or not output.is_file() or output.stat().st_size <= 44:
            raise SpeechSynthesisError("Piper erzeugte keine gültige Audiodatei.")

    def _load_voice(self) -> tuple[Any, Any]:
        if self._piper is None:
            self.preflight()
        if self._voice is None:
            assert self._piper is not None
            assert self.config.piper_model is not None
            config_path = self.config.piper_config or Path(f"{self.config.piper_model}.json")
            device_name = "CUDA" if self._using_cuda else "CPU"
            self.logger(f"Lade Piper-Stimme auf {device_name} …")
            load_options = {
                "model_path": str(self.config.piper_model),
                "config_path": str(config_path),
            }
            try:
                self._voice = self._piper.PiperVoice.load(
                    **load_options,
                    use_cuda=self._using_cuda,
                )
            except Exception as cuda_error:
                if self.config.tts_device != "auto" or not self._using_cuda:
                    raise
                self.logger(
                    "Piper-CUDA konnte nicht initialisiert werden; versuche das Modell auf CPU."
                )
                self._using_cuda = False
                try:
                    self._voice = self._piper.PiperVoice.load(
                        **load_options,
                        use_cuda=False,
                    )
                except Exception as cpu_error:
                    raise SpeechSynthesisError(
                        "Piper-Modell konnte weder auf CUDA noch auf CPU geladen werden: "
                        f"CUDA={cuda_error}; CPU={cpu_error}"
                    ) from cpu_error
        return self._voice, self._piper

    def _resolve_cuda(self) -> bool:
        if self.config.tts_device == "cpu":
            return False
        try:
            import onnxruntime
        except ImportError as exc:
            if self.config.tts_device == "cuda":
                raise DependencyError(
                    "Piper-CUDA benötigt onnxruntime-gpu in einer eigenen Umgebung."
                ) from exc
            return False
        if hasattr(onnxruntime, "preload_dlls"):
            with suppress(Exception):
                onnxruntime.preload_dlls()
        try:
            available = "CUDAExecutionProvider" in onnxruntime.get_available_providers()
        except (OSError, RuntimeError) as exc:
            if self.config.tts_device == "cuda":
                raise DependencyError(
                    f"ONNX Runtime konnte die CUDA-Provider nicht prüfen: {exc}"
                ) from exc
            return False
        if self.config.tts_device == "cuda" and not available:
            raise DependencyError(
                "Piper-CUDA ist nicht verfügbar. Installiere ausschließlich onnxruntime-gpu "
                "und prüfe CUDA/cuDNN."
            )
        return available

    def close(self) -> None:
        self._voice = None
        gc.collect()

    @staticmethod
    def _load_module() -> Any:
        try:
            import piper
        except (ImportError, OSError, RuntimeError) as exc:
            raise DependencyError(
                'Piper fehlt. Installiere die lokale Engine mit: pip install -e ".[piper]"'
            ) from exc
        return piper


class ChatterboxTTSSynthesizer:
    """Lokale mehrsprachige Stimmkopie aus einer autorisierten Referenzaufnahme."""

    def __init__(
        self,
        config: AppConfig,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or (lambda _message: None)
        self._model: Any | None = None
        self._torch: Any | None = None
        self._torchaudio: Any | None = None
        self._model_class: Any | None = None
        self._device: str | None = None

    def preflight(self) -> None:
        self._torch, self._torchaudio, self._model_class = self._load_modules()
        self._device = self._resolve_device(self._torch)
        self._validate_reference()

    def synthesize(
        self,
        segments: Sequence[Segment],
        output_directory: Path,
        *,
        cancel_event: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        if not segments:
            raise SpeechSynthesisError("Keine Segmente für TTS vorhanden.")
        check_cancelled(cancel_event)
        try:
            model, torchaudio = self._load_model()
        except (DependencyError, SpeechSynthesisError):
            raise
        except Exception as exc:
            raise SpeechSynthesisError(
                f"Chatterbox-Modell oder Stimmreferenz konnte nicht geladen werden: {exc}"
            ) from exc
        output_directory.mkdir(parents=True, exist_ok=True)
        language = self.config.target_language.split("-", maxsplit=1)[0].casefold()
        outputs: list[Path] = []
        for index, segment in enumerate(segments, start=1):
            check_cancelled(cancel_event)
            output = output_directory / f"voice-{index - 1:05d}.wav"
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial.wav")
            try:
                waveform, model = self._generate(segment.text, language, model)
                check_cancelled(cancel_event)
                torchaudio.save(str(temporary), waveform, model.sr)
                if not temporary.is_file() or temporary.stat().st_size <= 44:
                    raise SpeechSynthesisError("Chatterbox erzeugte keine gültige Audiodatei.")
                temporary.replace(output)
            except CancelledError:
                raise
            except Exception as exc:
                raise SpeechSynthesisError(
                    f"Chatterbox-TTS für Segment {index} ist fehlgeschlagen: {exc}"
                ) from exc
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            outputs.append(output)
            if on_progress:
                on_progress(index, len(segments))
        return outputs

    def _load_model(self) -> tuple[Any, Any]:
        if self._model_class is None or self._torchaudio is None:
            self.preflight()
        if self._model is None:
            assert self._model_class is not None
            assert self._torchaudio is not None
            assert self._device is not None
            assert self.config.tts_reference_audio is not None
            self.logger(
                f"Lade Chatterbox {self.config.chatterbox_model.upper()} auf "
                f"{self._device.upper()} (beim ersten Start mehrere GB) …"
            )
            # chatterbox-tts 0.1.7 exposes the published multilingual V2 model
            # through from_pretrained(device); the unreleased repository API has
            # an additional t3_model parameter and must not be assumed here.
            try:
                self._model = self._load_prepared_model(self._device)
            except Exception as cuda_error:
                if self.config.tts_device != "auto" or self._device != "cuda":
                    raise
                self._model = self._fallback_to_cpu(cuda_error, phase="initialisiert")
            self.logger("Stimmreferenz geladen; Chatterbox-Ausgaben enthalten ein Wasserzeichen.")
        return self._model, self._torchaudio

    def _load_prepared_model(self, device: str) -> Any:
        assert self._model_class is not None
        assert self.config.tts_reference_audio is not None
        model = self._model_class.from_pretrained(device=device)
        model.prepare_conditionals(
            str(self.config.tts_reference_audio),
            exaggeration=self.config.chatterbox_exaggeration,
        )
        return model

    def _fallback_to_cpu(self, cuda_error: Exception, *, phase: str) -> Any:
        self.logger(
            f"Chatterbox konnte auf CUDA nicht {phase} werden; versuche das Modell auf CPU."
        )
        self._model = None
        gc.collect()
        if self._torch is not None:
            with suppress(Exception):
                self._torch.cuda.empty_cache()
        self._device = "cpu"
        try:
            return self._load_prepared_model("cpu")
        except Exception as cpu_error:
            raise SpeechSynthesisError(
                "Chatterbox konnte weder auf CUDA noch auf CPU verwendet werden: "
                f"CUDA={cuda_error}; CPU={cpu_error}"
            ) from cpu_error

    def _generate(self, text: str, language: str, model: Any) -> tuple[Any, Any]:
        values = {
            "language_id": language,
            "audio_prompt_path": None,
            "exaggeration": self.config.chatterbox_exaggeration,
            "cfg_weight": self.config.chatterbox_cfg_weight,
        }
        try:
            return model.generate(text, **values), model
        except Exception as cuda_error:
            if self.config.tts_device != "auto" or self._device != "cuda":
                raise
            model = self._fallback_to_cpu(cuda_error, phase="für die Audioerzeugung verwendet")
            self._model = model
            return model.generate(text, **values), model

    def _resolve_device(self, torch: Any) -> str:
        if self.config.tts_device == "cpu":
            return "cpu"
        try:
            available = bool(torch.cuda.is_available())
        except (OSError, RuntimeError) as exc:
            if self.config.tts_device == "cuda":
                raise DependencyError(f"PyTorch-CUDA konnte nicht geprüft werden: {exc}") from exc
            return "cpu"
        if self.config.tts_device == "cuda" and not available:
            raise DependencyError(
                "Chatterbox-CUDA wurde gewählt, aber PyTorch erkennt keine CUDA-GPU."
            )
        return "cuda" if available else "cpu"

    def _validate_reference(self) -> None:
        """Prüft die Aufnahme vor Transkription und mehrgigabytegroßem Modelldownload."""
        assert self.config.tts_reference_audio is not None
        try:
            import librosa

            duration = float(librosa.get_duration(path=str(self.config.tts_reference_audio)))
        except Exception as exc:
            raise SpeechSynthesisError(
                f"Stimmreferenz ist nicht als Audio lesbar: {self.config.tts_reference_audio}"
            ) from exc
        if not math.isfinite(duration) or duration < 0.5:
            raise SpeechSynthesisError("Stimmreferenz enthält keine nutzbare Audioaufnahme.")
        if duration > 60:
            raise SpeechSynthesisError(
                "Stimmreferenz ist länger als 60 Sekunden. Verwende einen sauberen Ausschnitt "
                "von etwa 6 bis 10 Sekunden."
            )
        if not 4 <= duration <= 15:
            self.logger(
                f"Hinweis: Die Stimmreferenz ist {duration:.1f} Sekunden lang; empfohlen sind "
                "6 bis 10 Sekunden."
            )

    def close(self) -> None:
        self._model = None
        gc.collect()
        if self._torch is not None and self._device == "cuda":
            with suppress(Exception):
                self._torch.cuda.empty_cache()

    @staticmethod
    def _load_modules() -> tuple[Any, Any, Any]:
        try:
            import torch
            import torchaudio
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        except (ImportError, OSError, RuntimeError) as exc:
            raise DependencyError(
                "Chatterbox fehlt oder konnte nicht geladen werden. Verwende Python 3.11 und "
                'installiere: pip install -e ".[voice-clone]"'
            ) from exc
        return torch, torchaudio, ChatterboxMultilingualTTS


def create_synthesizer(
    config: AppConfig,
    *,
    logger: Callable[[str], None] | None = None,
) -> SpeechSynthesizer:
    """Erzeugt ausschließlich die explizit gewählte TTS-Engine."""
    factories = {
        "edge": EdgeTTSSynthesizer,
        "gtts": GoogleTTSSynthesizer,
        "piper": PiperTTSSynthesizer,
        "chatterbox": ChatterboxTTSSynthesizer,
    }
    try:
        factory = factories[config.tts_engine]
    except KeyError as exc:
        raise SpeechSynthesisError(f"Unbekannte TTS-Engine: {config.tts_engine!r}") from exc
    return factory(config, logger=logger)
