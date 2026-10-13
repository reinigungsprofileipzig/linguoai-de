"""UI-unabhängige, unveränderliche Anwendungskonfiguration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigurationError

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"})
OUTPUT_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov"})
TRANSLATION_SERVICES = ("google", "deepl", "gemini", "nllb")
TTS_ENGINES = ("edge", "gtts", "piper", "chatterbox")
TTS_DEVICES = ("auto", "cpu", "cuda")

# Chatterbox Multilingual uses base language codes. Regional application codes
# are reduced to their base code before synthesis.
CHATTERBOX_LANGUAGES = frozenset(
    {
        "ar",
        "da",
        "de",
        "el",
        "en",
        "es",
        "fi",
        "fr",
        "he",
        "hi",
        "it",
        "ja",
        "ko",
        "ms",
        "nl",
        "no",
        "pl",
        "pt",
        "ru",
        "sv",
        "sw",
        "tr",
        "zh",
    }
)

LANGUAGE_ALIASES = {
    "fil": "tl",
    "iw": "he",
    "nb": "no",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
}


def normalize_language_code(value: str) -> str:
    folded = value.strip().casefold()
    if folded in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[folded]
    parts = folded.split("-", maxsplit=1)
    if len(parts) == 1:
        return parts[0]
    suffix = parts[1].upper() if len(parts[1]) == 2 else parts[1].title()
    return f"{parts[0]}-{suffix}"


LANGUAGES: dict[str, str] = {
    "ar": "Arabisch",
    "bg": "Bulgarisch",
    "cs": "Tschechisch",
    "da": "Dänisch",
    "de": "Deutsch",
    "el": "Griechisch",
    "en": "Englisch",
    "es": "Spanisch",
    "fi": "Finnisch",
    "fr": "Französisch",
    "hi": "Hindi",
    "he": "Hebräisch",
    "hu": "Ungarisch",
    "id": "Indonesisch",
    "it": "Italienisch",
    "ja": "Japanisch",
    "ko": "Koreanisch",
    "ms": "Malaiisch",
    "nl": "Niederländisch",
    "no": "Norwegisch",
    "pl": "Polnisch",
    "pt": "Portugiesisch",
    "ro": "Rumänisch",
    "ru": "Russisch",
    "sv": "Schwedisch",
    "th": "Thai",
    "tl": "Filipino",
    "tr": "Türkisch",
    "uk": "Ukrainisch",
    "vi": "Vietnamesisch",
    "zh-CN": "Chinesisch (vereinfacht)",
}


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Alle Einstellungen eines Jobs, ohne Tkinter- oder CLI-Zustand."""

    target_language: str = "en"
    source_language: str | None = None
    translation_service: str = "google"
    fallback_services: tuple[str, ...] = ()
    whisper_model: str = "medium"
    device: str = "auto"
    compute_type: str = "default"
    transcription_workers: int = 2
    tts_workers: int = 4
    tts_engine: str = "edge"
    tts_device: str = "auto"
    tts_voice: str | None = None
    tts_reference_audio: Path | None = None
    confirm_voice_rights: bool = False
    piper_model: Path | None = None
    piper_config: Path | None = None
    piper_speaker_id: int | None = None
    piper_length_scale: float = 1.0
    chatterbox_model: str = "v2"
    chatterbox_exaggeration: float = 0.5
    chatterbox_cfg_weight: float = 0.5
    chunk_seconds: float = 120.0
    chunk_overlap_seconds: float = 1.25
    max_tts_speed: float = 2.2
    preferred_tts_speed: float = 1.35
    adaptive_timing: bool = True
    automatic_translation_shortening: bool = True
    context_aware_translation: bool = True
    translation_context_characters: int = 100_000
    optimize_transcript: bool = False
    gemini_model: str = "gemini-3.5-flash"
    gemini_api_key: str = field(default="", repr=False)
    deepl_api_key: str = field(default="", repr=False)
    nllb_model: str = "facebook/nllb-200-distilled-600M"
    nllb_device: str = "auto"
    nllb_batch_size: int = 8
    nllb_max_input_tokens: int = 512
    nllb_max_new_tokens: int = 256
    nllb_source_code: str | None = None
    nllb_target_code: str | None = None
    nllb_revision: str | None = None
    request_timeout_seconds: float = 60.0
    request_retries: int = 4
    request_retry_base_seconds: float = 2.0
    request_retry_max_seconds: float = 30.0
    gemini_request_pause_seconds: float = 2.5
    overwrite: bool = False
    temp_directory: Path | None = None

    def __post_init__(self) -> None:
        """Normalisiert nutzernahe Werte, ohne die Konfiguration veränderlich zu machen."""
        target = self.target_language.strip()
        source = self.source_language.strip() if self.source_language else None
        target = normalize_language_code(target)
        source = None if source and source.casefold() == "auto" else source
        if source:
            source = normalize_language_code(source)
        object.__setattr__(self, "target_language", target)
        object.__setattr__(self, "source_language", source)
        object.__setattr__(self, "translation_service", self.translation_service.strip().lower())
        object.__setattr__(
            self,
            "fallback_services",
            tuple(service.strip().lower() for service in self.fallback_services),
        )
        object.__setattr__(self, "whisper_model", self.whisper_model.strip())
        object.__setattr__(self, "device", self.device.strip().lower())
        object.__setattr__(self, "compute_type", self.compute_type.strip().lower())
        object.__setattr__(self, "tts_engine", self.tts_engine.strip().lower())
        object.__setattr__(self, "tts_device", self.tts_device.strip().lower())
        object.__setattr__(self, "gemini_model", self.gemini_model.strip())
        object.__setattr__(self, "gemini_api_key", self.gemini_api_key.strip())
        object.__setattr__(self, "deepl_api_key", self.deepl_api_key.strip())
        object.__setattr__(self, "nllb_model", self.nllb_model.strip())
        object.__setattr__(self, "nllb_device", self.nllb_device.strip().lower())
        if self.nllb_source_code is not None:
            object.__setattr__(self, "nllb_source_code", self.nllb_source_code.strip() or None)
        if self.nllb_target_code is not None:
            object.__setattr__(self, "nllb_target_code", self.nllb_target_code.strip() or None)
        if self.nllb_revision is not None:
            object.__setattr__(self, "nllb_revision", self.nllb_revision.strip() or None)
        if self.tts_voice is not None:
            object.__setattr__(self, "tts_voice", self.tts_voice.strip() or None)
        for attribute in ("tts_reference_audio", "piper_model", "piper_config"):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(self, attribute, Path(value).expanduser())
        object.__setattr__(self, "chatterbox_model", self.chatterbox_model.strip().lower())
        if self.temp_directory is not None:
            object.__setattr__(self, "temp_directory", Path(self.temp_directory).expanduser())

    def validated(self) -> AppConfig:
        """Validiert früh, bevor Modelle geladen oder Dateien verändert werden."""
        language_pattern = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")
        if not language_pattern.fullmatch(self.target_language):
            raise ConfigurationError(f"Ungültiger Zielsprachcode: {self.target_language!r}")
        if self.source_language and not language_pattern.fullmatch(self.source_language):
            raise ConfigurationError(f"Ungültiger Quellsprachcode: {self.source_language!r}")
        if self.translation_service not in TRANSLATION_SERVICES:
            raise ConfigurationError(
                f"Unbekannter Übersetzungsdienst: {self.translation_service!r}"
            )
        unknown_fallbacks = set(self.fallback_services) - set(TRANSLATION_SERVICES)
        if unknown_fallbacks:
            raise ConfigurationError(
                "Unbekannte Fallback-Dienste: " + ", ".join(sorted(unknown_fallbacks))
            )
        if self.translation_service in self.fallback_services:
            raise ConfigurationError("Der Primärdienst darf nicht zugleich Fallback sein.")
        if len(set(self.fallback_services)) != len(self.fallback_services):
            raise ConfigurationError("Fallback-Dienste dürfen nicht doppelt vorkommen.")

        required_services = {self.translation_service, *self.fallback_services}
        if "deepl" in required_services and not self.deepl_api_key.strip():
            raise ConfigurationError("DeepL ist gewählt, aber DEEPL_API_KEY fehlt.")
        if "gemini" in required_services and not self.gemini_api_key.strip():
            raise ConfigurationError("Gemini ist gewählt, aber GEMINI_API_KEY fehlt.")
        if self.optimize_transcript and not self.gemini_api_key.strip():
            raise ConfigurationError(
                "Textoptimierung benötigt einen GEMINI_API_KEY oder muss deaktiviert werden."
            )
        needs_optional_gemini = self.optimize_transcript or (
            self.automatic_translation_shortening and bool(self.gemini_api_key.strip())
        )
        if ("gemini" in required_services or needs_optional_gemini) and not self.gemini_model:
            raise ConfigurationError("Gemini-Modell darf nicht leer sein.")
        if "nllb" in required_services and not self.nllb_model:
            raise ConfigurationError("NLLB-Modell oder lokaler Modellpfad darf nicht leer sein.")
        flores_pattern = re.compile(r"^[a-z]{3}_[A-Z][a-z]{3}$")
        for label, code in (
            ("NLLB-Quellcode", self.nllb_source_code),
            ("NLLB-Zielcode", self.nllb_target_code),
        ):
            if code and not flores_pattern.fullmatch(code):
                raise ConfigurationError(f"Ungültiger {label}: {code!r}")

        if self.device not in {"auto", "cpu", "cuda"}:
            raise ConfigurationError("Gerät muss 'auto', 'cpu' oder 'cuda' sein.")
        if self.tts_engine not in TTS_ENGINES:
            raise ConfigurationError(f"Unbekannte TTS-Engine: {self.tts_engine!r}")
        if self.tts_device not in TTS_DEVICES:
            raise ConfigurationError("TTS-Gerät muss 'auto', 'cpu' oder 'cuda' sein.")
        self._validate_tts()
        if self.nllb_device not in {"auto", "cpu", "cuda"}:
            raise ConfigurationError("NLLB-Gerät muss 'auto', 'cpu' oder 'cuda' sein.")
        if not self.whisper_model.strip():
            raise ConfigurationError("Whisper-Modell darf nicht leer sein.")
        if not 1 <= self.transcription_workers <= 8:
            raise ConfigurationError("Transkriptions-Worker müssen zwischen 1 und 8 liegen.")
        if not 1 <= self.tts_workers <= 16:
            raise ConfigurationError("TTS-Worker müssen zwischen 1 und 16 liegen.")
        if not 1 <= self.nllb_batch_size <= 64:
            raise ConfigurationError("NLLB-Batchgröße muss zwischen 1 und 64 liegen.")
        if not 32 <= self.nllb_max_input_tokens <= 512:
            raise ConfigurationError("NLLB-Eingabelimit muss zwischen 32 und 512 Tokens liegen.")
        if not 16 <= self.nllb_max_new_tokens <= 512:
            raise ConfigurationError("NLLB-Ausgabelimit muss zwischen 16 und 512 Tokens liegen.")
        if not math.isfinite(self.chunk_seconds) or self.chunk_seconds < 20:
            raise ConfigurationError("Audio-Chunks müssen mindestens 20 Sekunden lang sein.")
        if not math.isfinite(self.chunk_overlap_seconds) or not (
            0 <= self.chunk_overlap_seconds < self.chunk_seconds / 2
        ):
            raise ConfigurationError("Chunk-Überlappung ist außerhalb des gültigen Bereichs.")
        if not math.isfinite(self.max_tts_speed) or not 1.0 <= self.max_tts_speed <= 4.0:
            raise ConfigurationError("Maximales TTS-Tempo muss zwischen 1,0 und 4,0 liegen.")
        if not math.isfinite(self.preferred_tts_speed) or not (
            1.0 <= self.preferred_tts_speed <= self.max_tts_speed
        ):
            raise ConfigurationError(
                "Bevorzugtes TTS-Tempo muss zwischen 1,0 und dem Maximaltempo liegen."
            )
        if not 1_000 <= self.translation_context_characters <= 1_000_000:
            raise ConfigurationError(
                "Übersetzungskontext muss zwischen 1.000 und 1.000.000 Zeichen liegen."
            )
        if not math.isfinite(self.request_timeout_seconds) or self.request_timeout_seconds <= 0:
            raise ConfigurationError("Request-Timeout muss positiv sein.")
        if not 1 <= self.request_retries <= 8:
            raise ConfigurationError("Request-Wiederholungen müssen zwischen 1 und 8 liegen.")
        if not math.isfinite(self.request_retry_base_seconds) or not (
            0.0 <= self.request_retry_base_seconds <= 60.0
        ):
            raise ConfigurationError(
                "Basis-Pause für Retries muss zwischen 0 und 60 Sekunden liegen."
            )
        if not math.isfinite(self.request_retry_max_seconds) or not (
            self.request_retry_base_seconds <= self.request_retry_max_seconds <= 300.0
        ):
            raise ConfigurationError(
                "Maximale Retry-Pause muss zwischen Basis-Pause und 300 Sekunden liegen."
            )
        if not math.isfinite(self.gemini_request_pause_seconds) or not (
            0.0 <= self.gemini_request_pause_seconds <= 60.0
        ):
            raise ConfigurationError("Gemini-Anfragepause muss zwischen 0 und 60 Sekunden liegen.")
        if self.temp_directory is not None:
            if not self.temp_directory.exists():
                raise ConfigurationError(
                    f"Temporäres Verzeichnis existiert nicht: {self.temp_directory}"
                )
            if not self.temp_directory.is_dir():
                raise ConfigurationError(
                    f"Temporärer Pfad ist kein Verzeichnis: {self.temp_directory}"
                )
        return self

    def _validate_tts(self) -> None:
        if self.piper_speaker_id is not None and self.piper_speaker_id < 0:
            raise ConfigurationError("Piper-Sprecher-ID darf nicht negativ sein.")
        if not math.isfinite(self.piper_length_scale) or not 0.25 <= self.piper_length_scale <= 4:
            raise ConfigurationError("Piper-Längenfaktor muss zwischen 0,25 und 4,0 liegen.")
        if not math.isfinite(self.chatterbox_exaggeration) or not (
            0 <= self.chatterbox_exaggeration <= 2
        ):
            raise ConfigurationError("Chatterbox-Ausdruck muss zwischen 0 und 2 liegen.")
        if not math.isfinite(self.chatterbox_cfg_weight) or not (
            0 <= self.chatterbox_cfg_weight <= 1
        ):
            raise ConfigurationError("Chatterbox-CFG muss zwischen 0 und 1 liegen.")

        if self.tts_engine == "piper":
            if self.piper_model is None:
                raise ConfigurationError("Piper benötigt ein lokales .onnx-Stimmenmodell.")
            if self.piper_model.suffix.casefold() != ".onnx":
                raise ConfigurationError("Das Piper-Stimmenmodell muss eine .onnx-Datei sein.")
            self._require_file(self.piper_model, "Piper-Stimmenmodell")
            config_path = self.piper_config or Path(f"{self.piper_model}.json")
            self._require_file(config_path, "Piper-Modellkonfiguration")

        if self.tts_engine == "chatterbox":
            if self.chatterbox_model != "v2":
                raise ConfigurationError(
                    "Diese stabile Chatterbox-Integration unterstützt das veröffentlichte "
                    "Multilingual-V2-Modell."
                )
            if self.tts_reference_audio is None:
                raise ConfigurationError(
                    "Chatterbox benötigt eine Referenzaufnahme der gewünschten Stimme."
                )
            self._require_file(self.tts_reference_audio, "Stimmreferenz")
            if not self.confirm_voice_rights:
                raise ConfigurationError(
                    "Bestätige, dass du die Erlaubnis zur Nutzung der Referenzstimme hast."
                )
            language = self.target_language.split("-", maxsplit=1)[0].casefold()
            if language not in CHATTERBOX_LANGUAGES:
                raise ConfigurationError(
                    f"Chatterbox Multilingual unterstützt die Zielsprache "
                    f"{self.target_language!r} nicht."
                )

    @staticmethod
    def _require_file(path: Path, label: str) -> None:
        if not path.exists():
            raise ConfigurationError(f"{label} wurde nicht gefunden: {path}")
        if not path.is_file():
            raise ConfigurationError(f"{label} ist keine Datei: {path}")
