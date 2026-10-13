"""Lokale NLLB-Übersetzung für CPU, CUDA und Google Colab."""

from __future__ import annotations

import gc
import re
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from .errors import CancelledError, DependencyError, TranslationError
from .runtime import check_cancelled

# BCP-47/Whisper-Codes auf FLORES-200-Codes des NLLB-Tokenizers.
NLLB_LANGUAGE_MAP: dict[str, str] = {
    "ar": "arb_Arab",
    "bg": "bul_Cyrl",
    "cs": "ces_Latn",
    "da": "dan_Latn",
    "de": "deu_Latn",
    "el": "ell_Grek",
    "en": "eng_Latn",
    "es": "spa_Latn",
    "fi": "fin_Latn",
    "fr": "fra_Latn",
    "he": "heb_Hebr",
    "fa": "pes_Arab",
    "hi": "hin_Deva",
    "hu": "hun_Latn",
    "id": "ind_Latn",
    "it": "ita_Latn",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "ms": "zsm_Latn",
    "nl": "nld_Latn",
    "no": "nob_Latn",
    "nn": "nno_Latn",
    "pl": "pol_Latn",
    "pt": "por_Latn",
    "ro": "ron_Latn",
    "ru": "rus_Cyrl",
    "sv": "swe_Latn",
    "th": "tha_Thai",
    "tl": "tgl_Latn",
    "tr": "tur_Latn",
    "uk": "ukr_Cyrl",
    "vi": "vie_Latn",
    "zh": "zho_Hans",
    "zh-CN": "zho_Hans",
    "zh-Hans": "zho_Hans",
    "zh-SG": "zho_Hans",
    "zh-Hant": "zho_Hant",
    "zh-HK": "zho_Hant",
    "zh-MO": "zho_Hant",
    "zh-TW": "zho_Hant",
}

_PROTECTED_PLACEHOLDER = re.compile(r"__LINGUO_[A-F0-9]{32}_\d{4}__")


class _CudaDeviceLoadError(RuntimeError):
    """Markiert ausschließlich Fehler beim Transfer eines geladenen Modells auf CUDA."""


class NllbTranslationAdapter:
    """Lädt ein NLLB-kompatibles Hugging-Face-Modell erst bei Bedarf."""

    name = "nllb"

    def __init__(
        self,
        *,
        model_name: str,
        device: str = "auto",
        batch_size: int = 8,
        max_input_tokens: int = 512,
        max_new_tokens: int = 256,
        target_language: str = "en",
        source_code_override: str | None = None,
        target_code_override: str | None = None,
        revision: str | None = None,
        allow_cpu_fallback: bool = True,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self.batch_size = batch_size
        self.max_input_tokens = max_input_tokens
        self.max_new_tokens = max_new_tokens
        self.target_language = target_language
        self.source_code_override = source_code_override
        self.target_code_override = target_code_override
        self.revision = revision
        self.allow_cpu_fallback = allow_cpu_fallback
        self.logger = logger or (lambda _message: None)
        self._torch: Any | None = None
        self._transformers: Any | None = None
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._device: str | None = None
        self._lock = threading.RLock()

    @property
    def resolved_device(self) -> str | None:
        return self._device

    def preflight(self) -> None:
        torch, _transformers = self._load_dependencies()
        if self.requested_device == "cuda" and not torch.cuda.is_available():
            raise TranslationError(
                "CUDA wurde für NLLB angefordert, ist in dieser Laufzeit aber nicht verfügbar."
            )
        self._target_language_code(
            self.target_language,
        )

    def translate_batch(
        self,
        texts: Sequence[str],
        *,
        source_language: str | None,
        target_language: str,
        cancel_event: threading.Event | None = None,
    ) -> list[str]:
        if not texts:
            return []
        active_cancel = cancel_event or threading.Event()
        check_cancelled(active_cancel)
        source_code = self._language_code(
            source_language,
            role="Quellsprache",
            override=self.source_code_override,
        )
        target_code = self._target_language_code(target_language)
        with self._lock:
            self._ensure_model(source_code, target_code, active_cancel)
            check_cancelled(active_cancel)
            self._set_source_language(source_code)

            translated: list[str] = []
            for start in range(0, len(texts), self.batch_size):
                check_cancelled(active_cancel)
                translated.extend(
                    self._translate_group(
                        list(texts[start : start + self.batch_size]),
                        target_code,
                        active_cancel,
                    )
                )
            return translated

    def close(self) -> None:
        self._model = None
        self._tokenizer = None
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()

    @staticmethod
    def is_retryable(error: BaseException) -> bool:
        """Wiederholt nur transiente Hub-/Netzwerkfehler, keine Modell- oder Sprachfehler."""
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            status = getattr(current, "status_code", None)
            response = getattr(current, "response", None)
            if status is None and response is not None:
                status = getattr(response, "status_code", None)
            try:
                status_code = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_code = None
            if status_code is not None and (status_code in {408, 429} or status_code >= 500):
                return True
            name = type(current).__name__.casefold()
            if any(marker in name for marker in ("timeout", "connect", "network", "protocol")):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _load_dependencies(self) -> tuple[Any, Any]:
        if self._torch is not None and self._transformers is not None:
            return self._torch, self._transformers
        try:
            import torch
            import transformers
        except ImportError as exc:
            raise DependencyError(
                "Lokale NLLB-Übersetzung fehlt. Installiere sie mit 'pip install -e \".[local]\"'."
            ) from exc
        self._torch = torch
        self._transformers = transformers
        return torch, transformers

    def _ensure_model(
        self,
        source_code: str,
        target_code: str,
        cancel_event: threading.Event,
    ) -> None:
        if self._model is not None:
            return
        torch, transformers = self._load_dependencies()
        if self.requested_device == "auto":
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self._device = self.requested_device
        if self._device == "cuda" and not torch.cuda.is_available():
            raise TranslationError("NLLB kann CUDA in dieser Laufzeit nicht verwenden.")

        check_cancelled(cancel_event)
        try:
            self._tokenizer = transformers.AutoTokenizer.from_pretrained(
                self.model_name,
                src_lang=source_code,
                revision=self.revision,
                trust_remote_code=False,
            )
            check_cancelled(cancel_event)
        except CancelledError:
            raise
        except Exception as exc:
            self._tokenizer = None
            raise TranslationError(
                f"NLLB-Tokenizer für {self.model_name!r} konnte nicht geladen werden "
                f"({type(exc).__name__})."
            ) from exc
        self._validate_language_token(source_code, role="Quellcode")
        self._validate_language_token(target_code, role="Zielcode")

        first_device = self._device
        self.logger(f"Lade NLLB-Modell {self.model_name!r} auf {first_device.upper()} …")
        try:
            self._load_weights_on_device(first_device, cancel_event)
            return
        except CancelledError:
            raise
        except Exception as first_error:
            self._release_model_memory()
            if not (
                first_device == "cuda"
                and self.requested_device == "auto"
                and self.allow_cpu_fallback
                and isinstance(first_error, _CudaDeviceLoadError)
            ):
                raise TranslationError(
                    f"NLLB-Modell {self.model_name!r} konnte auf {first_device.upper()} "
                    f"nicht geladen werden ({type(first_error).__name__})."
                ) from first_error

        self._device = "cpu"
        self.logger("NLLB-CUDA konnte nicht geladen werden; versuche CPU/FP32.")
        check_cancelled(cancel_event)
        try:
            self._load_weights_on_device("cpu", cancel_event)
        except CancelledError:
            raise
        except Exception as cpu_error:
            self._release_model_memory()
            raise TranslationError(
                f"NLLB-Modell {self.model_name!r} konnte weder auf CUDA noch auf CPU "
                f"geladen werden (CPU: {type(cpu_error).__name__})."
            ) from cpu_error

    def _load_weights_on_device(
        self,
        device: str,
        cancel_event: threading.Event,
    ) -> None:
        torch = self._torch
        transformers = self._transformers
        dtype = torch.float16 if device == "cuda" else torch.float32
        model = transformers.AutoModelForSeq2SeqLM.from_pretrained(
            self.model_name,
            revision=self.revision,
            dtype=dtype,
            low_cpu_mem_usage=True,
            trust_remote_code=False,
        )
        check_cancelled(cancel_event)
        try:
            model.to(device)
        except Exception as exc:
            if device == "cuda":
                raise _CudaDeviceLoadError from exc
            raise
        check_cancelled(cancel_event)
        model.eval()
        self._model = model
        self._device = device

    def _release_model_memory(self) -> None:
        self._model = None
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            try:
                self._torch.cuda.empty_cache()
            except (RuntimeError, OSError):
                return

    def _set_source_language(self, source_code: str) -> None:
        self._validate_language_token(source_code, role="Quellcode")
        self._tokenizer.src_lang = source_code
        setter = getattr(self._tokenizer, "set_src_lang_special_tokens", None)
        if setter:
            setter(source_code)

    def _validate_language_token(self, code: str, *, role: str) -> int:
        token = self._tokenizer.convert_tokens_to_ids(code)
        if token is None or token == self._tokenizer.unk_token_id:
            raise TranslationError(f"NLLB kennt den {role} {code!r} nicht.")
        return int(token)

    def _translate_group(
        self,
        texts: list[str],
        target_code: str,
        cancel_event: threading.Event,
    ) -> list[str]:
        torch = self._torch
        transformers = self._transformers
        tokenizer = self._tokenizer
        model = self._model
        check_cancelled(cancel_event)
        try:
            encoded = tokenizer(
                texts,
                return_tensors="pt",
                padding=True,
                truncation=False,
            )
            longest = int(encoded["attention_mask"].sum(dim=1).max().item())
            if longest > self.max_input_tokens:
                raise TranslationError(
                    f"NLLB-Eingabe hat {longest} Tokens; erlaubt sind höchstens "
                    f"{self.max_input_tokens}."
                )
            encoded = {name: tensor.to(self._device) for name, tensor in encoded.items()}
            target_token = self._validate_language_token(target_code, role="Zielcode")

            class CancellationCriteria(transformers.StoppingCriteria):
                def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> Any:
                    return torch.full(
                        (input_ids.shape[0],),
                        cancel_event.is_set(),
                        dtype=torch.bool,
                        device=input_ids.device,
                    )

            with torch.inference_mode():
                output = model.generate(
                    **encoded,
                    forced_bos_token_id=target_token,
                    max_new_tokens=self.max_new_tokens,
                    num_beams=1,
                    do_sample=False,
                    return_dict_in_generate=False,
                    stopping_criteria=transformers.StoppingCriteriaList([CancellationCriteria()]),
                )
            check_cancelled(cancel_event)
            sequences = getattr(output, "sequences", output)
            eos_token_id = tokenizer.eos_token_id
            if eos_token_id is not None and any(
                not self._sequence_has_generated_eos(row, eos_token_id) for row in sequences
            ):
                raise TranslationError(
                    "NLLB-Ausgabe erreichte das Tokenlimit ohne Satzende. "
                    "Erhöhe --nllb-max-new-tokens."
                )
            decoded = [
                str(item).strip()
                for item in tokenizer.batch_decode(sequences, skip_special_tokens=True)
            ]
            if len(decoded) != len(texts) or any(not item for item in decoded):
                raise TranslationError("NLLB lieferte leere oder fehlende Segmente.")
            for index, (source, candidate) in enumerate(zip(texts, decoded, strict=True)):
                if not self._placeholders_preserved(source, candidate):
                    self.logger(
                        "NLLB veränderte einen Schutzmarker; übersetze die Textteile "
                        "noch einmal mit sicherer Wiedereinfügung."
                    )
                    decoded[index] = self._translate_around_placeholders(
                        source,
                        target_code,
                        cancel_event,
                    )
            return decoded
        except RuntimeError as exc:
            is_oom = "out of memory" in str(exc).casefold()
            if is_oom and len(texts) > 1:
                self.logger(f"NLLB-Speicher knapp; reduziere Batch von {len(texts)} automatisch.")
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                middle = len(texts) // 2
                return self._translate_group(
                    texts[:middle], target_code, cancel_event
                ) + self._translate_group(texts[middle:], target_code, cancel_event)
            if (
                is_oom
                and self._device == "cuda"
                and self.allow_cpu_fallback
                and self._model is not None
            ):
                self.logger("NLLB-GPU-Speicher reicht nicht; wechsle automatisch auf CPU.")
                self._model.to("cpu")
                self._model.float()
                self._device = "cpu"
                torch.cuda.empty_cache()
                return self._translate_group(texts, target_code, cancel_event)
            hint = " Reduziere --nllb-batch-size." if is_oom else ""
            raise TranslationError(f"Lokale NLLB-Übersetzung fehlgeschlagen.{hint}") from exc

    @staticmethod
    def _sequence_has_generated_eos(sequence: Any, eos_token_id: Any) -> bool:
        tokens = sequence.tolist() if hasattr(sequence, "tolist") else list(sequence)
        eos_ids = (
            {int(item) for item in eos_token_id}
            if isinstance(eos_token_id, (list, tuple, set))
            else {int(eos_token_id)}
        )
        # Bei NLLB ist decoder_start_token_id häufig selbst die EOS-ID. Nur
        # Tokens nach diesem Startwert belegen ein reguläres Generationsende.
        return bool(eos_ids.intersection(int(token) for token in tokens[1:]))

    @staticmethod
    def _placeholders_preserved(source: str, translated: str) -> bool:
        return Counter(_PROTECTED_PLACEHOLDER.findall(source)) == Counter(
            _PROTECTED_PLACEHOLDER.findall(translated)
        )

    def _translate_around_placeholders(
        self,
        text: str,
        target_code: str,
        cancel_event: threading.Event,
    ) -> str:
        parts = _PROTECTED_PLACEHOLDER.split(text)
        markers = _PROTECTED_PLACEHOLDER.findall(text)
        translatable_indexes = [
            index
            for index, part in enumerate(parts)
            if part.strip() and any(character.isalnum() for character in part)
        ]
        if translatable_indexes:
            fragments = [parts[index].strip() for index in translatable_indexes]
            translated_fragments = self._translate_group(
                fragments,
                target_code,
                cancel_event,
            )
            for index, translated in zip(
                translatable_indexes,
                translated_fragments,
                strict=True,
            ):
                original = parts[index]
                prefix = " " if original[:1].isspace() else ""
                suffix = " " if original[-1:].isspace() else ""
                parts[index] = f"{prefix}{translated.strip()}{suffix}"

        rebuilt: list[str] = []
        for index, part in enumerate(parts):
            rebuilt.append(part)
            if index < len(markers):
                rebuilt.append(markers[index])
        return "".join(rebuilt).strip()

    @staticmethod
    def _language_code(
        language: str | None,
        *,
        role: str,
        override: str | None = None,
    ) -> str:
        if override:
            return override
        if not language:
            raise TranslationError(
                f"NLLB benötigt eine erkannte oder explizit gesetzte {role.lower()}."
            )
        exact = NLLB_LANGUAGE_MAP.get(language)
        if exact:
            return exact
        base = language.split("-", maxsplit=1)[0]
        if base in NLLB_LANGUAGE_MAP:
            return NLLB_LANGUAGE_MAP[base]
        raise TranslationError(
            f"{role} {language!r} ist in der integrierten NLLB-Zuordnung nicht enthalten."
        )

    def _target_language_code(self, language: str) -> str:
        exact_or_base = NLLB_LANGUAGE_MAP.get(language) or NLLB_LANGUAGE_MAP.get(
            language.split("-", maxsplit=1)[0]
        )
        if (
            self.target_code_override
            and exact_or_base
            and self.target_code_override != exact_or_base
        ):
            raise TranslationError(
                f"NLLB-Zielcode {self.target_code_override!r} widerspricht dem "
                f"Zielsprachcode {language!r} ({exact_or_base})."
            )
        return self._language_code(
            language,
            role="Zielsprache",
            override=self.target_code_override,
        )
