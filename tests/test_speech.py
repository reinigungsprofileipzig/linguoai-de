import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from linguoai.config import AppConfig
from linguoai.domain import Segment
from linguoai.errors import CancelledError, SpeechSynthesisError
from linguoai.speech import (
    ChatterboxTTSSynthesizer,
    EdgeTTSSynthesizer,
    GoogleTTSSynthesizer,
    PiperTTSSynthesizer,
    create_synthesizer,
)


class _PiperChunk:
    sample_rate = 22_050
    sample_width = 2
    sample_channels = 1
    audio_int16_bytes = b"\x00\x00" * 160


class _FakePiperVoice:
    calls: ClassVar[list[tuple[str, object]]] = []

    def synthesize(self, text, *, syn_config):
        type(self).calls.append((text, syn_config))
        yield _PiperChunk()


class _FakePiper:
    loaded = 0

    class SynthesisConfig:
        def __init__(self, **values):
            self.values = values

    class PiperVoice:
        @staticmethod
        def load(**_values):
            _FakePiper.loaded += 1
            return _FakePiperVoice()


class _FakeCuda:
    @staticmethod
    def is_available():
        return False

    @staticmethod
    def empty_cache():
        return None


class _FakeTorch:
    cuda = _FakeCuda()


class _FakeTorchaudio:
    @staticmethod
    def save(path, _waveform, _sample_rate):
        Path(path).write_bytes(b"RIFF" + b"\x00" * 64)


class _FakeChatterboxModel:
    sr = 24_000

    def __init__(self):
        self.prepared: list[tuple[str, float]] = []
        self.generated: list[dict[str, object]] = []

    def prepare_conditionals(self, path, *, exaggeration):
        self.prepared.append((path, exaggeration))

    def generate(self, text, **values):
        self.generated.append({"text": text, **values})
        return object()


class _FakeChatterboxClass:
    loaded: ClassVar[list[str]] = []
    instance: _FakeChatterboxModel | None = None

    @classmethod
    def from_pretrained(cls, *, device):
        cls.loaded.append(device)
        cls.instance = _FakeChatterboxModel()
        return cls.instance


class _FakeGTTS:
    calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, **values):
        self.values = values
        type(self).calls.append(values)

    def save(self, path):
        Path(path).write_bytes(b"ID3" + b"\x00" * 160)


class SpeechSynthesizerTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePiper.loaded = 0
        _FakePiperVoice.calls = []
        _FakeGTTS.calls = []
        _FakeChatterboxClass.loaded = []
        _FakeChatterboxClass.instance = None

    def test_factory_keeps_edge_as_the_compatible_default(self) -> None:
        synthesizer = create_synthesizer(AppConfig())
        self.assertIsInstance(synthesizer, EdgeTTSSynthesizer)

    def test_factory_can_create_gtts_synthesizer(self) -> None:
        synthesizer = create_synthesizer(AppConfig(tts_engine="gtts"))
        self.assertIsInstance(synthesizer, GoogleTTSSynthesizer)

    def test_gtts_writes_mp3_files_and_uses_voice_language_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synthesizer = GoogleTTSSynthesizer(
                AppConfig(target_language="de", tts_engine="gtts", tts_voice="en").validated()
            )
            synthesizer._load_module = lambda: _FakeGTTS  # type: ignore[method-assign]
            progress: list[tuple[int, int]] = []

            outputs = synthesizer.synthesize(
                (Segment(0, 1, "Hello"), Segment(1, 2, "World")),
                root / "out",
                cancel_event=threading.Event(),
                on_progress=lambda done, total: progress.append((done, total)),
            )

            self.assertEqual(progress, [(1, 2), (2, 2)])
            self.assertEqual([call["lang"] for call in _FakeGTTS.calls], ["en", "en"])
            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(output.suffix == ".mp3" for output in outputs))
            self.assertTrue(all(output.is_file() for output in outputs))

    def test_piper_reuses_one_model_and_writes_valid_wav_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "voice.onnx"
            model.write_bytes(b"model")
            Path(f"{model}.json").write_text("{}", encoding="utf-8")
            config = AppConfig(
                tts_engine="piper",
                tts_device="cpu",
                piper_model=model,
                piper_speaker_id=2,
                piper_length_scale=1.2,
            ).validated()
            synthesizer = PiperTTSSynthesizer(config)
            synthesizer._piper = _FakePiper()
            progress: list[tuple[int, int]] = []

            outputs = synthesizer.synthesize(
                (
                    Segment(0, 1, "Hallo"),
                    Segment(1, 2, "Welt"),
                ),
                root / "out",
                cancel_event=threading.Event(),
                on_progress=lambda done, total: progress.append((done, total)),
            )

            self.assertEqual(_FakePiper.loaded, 1)
            self.assertEqual(progress, [(1, 2), (2, 2)])
            self.assertEqual(len(outputs), 2)
            self.assertEqual(_FakePiperVoice.calls[0][1].values["speaker_id"], 2)
            for output in outputs:
                with wave.open(str(output), "rb") as wav_file:
                    self.assertEqual(wav_file.getframerate(), 22_050)
                    self.assertGreater(wav_file.getnframes(), 0)

    def test_chatterbox_prepares_reference_only_once_for_all_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            reference.write_bytes(b"reference")
            config = AppConfig(
                target_language="zh-CN",
                tts_engine="chatterbox",
                tts_device="cpu",
                tts_reference_audio=reference,
                confirm_voice_rights=True,
            ).validated()
            synthesizer = ChatterboxTTSSynthesizer(config)
            synthesizer._torch = _FakeTorch()
            synthesizer._torchaudio = _FakeTorchaudio()
            synthesizer._model_class = _FakeChatterboxClass
            synthesizer._device = "cpu"

            outputs = synthesizer.synthesize(
                (Segment(0, 1, "Eins"), Segment(1, 2, "Zwei")),
                root / "out",
                cancel_event=threading.Event(),
            )

            instance = _FakeChatterboxClass.instance
            assert instance is not None
            self.assertEqual(_FakeChatterboxClass.loaded, ["cpu"])
            self.assertEqual(len(instance.prepared), 1)
            self.assertEqual(len(instance.generated), 2)
            self.assertEqual(instance.generated[0]["language_id"], "zh")
            self.assertTrue(all(path.is_file() for path in outputs))

    def test_piper_auto_falls_back_to_cpu_if_cuda_session_fails(self) -> None:
        class AutoFallbackPiper(_FakePiper):
            devices: ClassVar[list[bool]] = []

            class PiperVoice:
                @staticmethod
                def load(*, use_cuda, **_values):
                    AutoFallbackPiper.devices.append(use_cuda)
                    if use_cuda:
                        raise RuntimeError("CUDA provider failed")
                    return _FakePiperVoice()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "voice.onnx"
            model.write_bytes(b"model")
            Path(f"{model}.json").write_text("{}", encoding="utf-8")
            synthesizer = PiperTTSSynthesizer(
                AppConfig(tts_engine="piper", tts_device="auto", piper_model=model).validated()
            )
            synthesizer._piper = AutoFallbackPiper()
            synthesizer._using_cuda = True

            outputs = synthesizer.synthesize(
                (Segment(0, 1, "Hallo"),),
                root / "out",
                cancel_event=threading.Event(),
            )

            self.assertEqual(AutoFallbackPiper.devices, [True, False])
            self.assertTrue(outputs[0].is_file())

    def test_chatterbox_auto_falls_back_to_cpu_if_cuda_load_fails(self) -> None:
        class AutoFallbackChatterbox:
            devices: ClassVar[list[str]] = []

            @classmethod
            def from_pretrained(cls, *, device):
                cls.devices.append(device)
                if device == "cuda":
                    raise RuntimeError("CUDA out of memory")
                return _FakeChatterboxModel()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            reference.write_bytes(b"reference")
            synthesizer = ChatterboxTTSSynthesizer(
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_device="auto",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated()
            )
            synthesizer._torch = _FakeTorch()
            synthesizer._torchaudio = _FakeTorchaudio()
            synthesizer._model_class = AutoFallbackChatterbox
            synthesizer._device = "cuda"

            outputs = synthesizer.synthesize(
                (Segment(0, 1, "Hallo"),),
                root / "out",
                cancel_event=threading.Event(),
            )

            self.assertEqual(AutoFallbackChatterbox.devices, ["cuda", "cpu"])
            self.assertTrue(outputs[0].is_file())

    def test_chatterbox_auto_falls_back_if_cuda_reference_preparation_fails(self) -> None:
        class PrepareFallbackModel(_FakeChatterboxModel):
            def __init__(self, device: str):
                super().__init__()
                self.device = device

            def prepare_conditionals(self, path, *, exaggeration):
                if self.device == "cuda":
                    raise RuntimeError("CUDA out of memory while preparing")
                super().prepare_conditionals(path, exaggeration=exaggeration)

        class PrepareFallbackChatterbox:
            devices: ClassVar[list[str]] = []

            @classmethod
            def from_pretrained(cls, *, device):
                cls.devices.append(device)
                return PrepareFallbackModel(device)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            reference.write_bytes(b"reference")
            synthesizer = ChatterboxTTSSynthesizer(
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_device="auto",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated()
            )
            synthesizer._torch = _FakeTorch()
            synthesizer._torchaudio = _FakeTorchaudio()
            synthesizer._model_class = PrepareFallbackChatterbox
            synthesizer._device = "cuda"

            outputs = synthesizer.synthesize(
                (Segment(0, 1, "Hallo"),),
                root / "out",
                cancel_event=threading.Event(),
            )

            self.assertEqual(PrepareFallbackChatterbox.devices, ["cuda", "cpu"])
            self.assertTrue(outputs[0].is_file())

    def test_chatterbox_auto_retries_generation_on_cpu_after_cuda_failure(self) -> None:
        class GenerateFallbackModel(_FakeChatterboxModel):
            def __init__(self, device: str):
                super().__init__()
                self.device = device

            def generate(self, text, **values):
                if self.device == "cuda":
                    raise RuntimeError("CUDA out of memory while generating")
                return super().generate(text, **values)

        class GenerateFallbackChatterbox:
            devices: ClassVar[list[str]] = []

            @classmethod
            def from_pretrained(cls, *, device):
                cls.devices.append(device)
                return GenerateFallbackModel(device)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            reference.write_bytes(b"reference")
            synthesizer = ChatterboxTTSSynthesizer(
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_device="auto",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated()
            )
            synthesizer._torch = _FakeTorch()
            synthesizer._torchaudio = _FakeTorchaudio()
            synthesizer._model_class = GenerateFallbackChatterbox
            synthesizer._device = "cuda"

            outputs = synthesizer.synthesize(
                (Segment(0, 1, "Hallo"),),
                root / "out",
                cancel_event=threading.Event(),
            )

            self.assertEqual(GenerateFallbackChatterbox.devices, ["cuda", "cpu"])
            self.assertTrue(outputs[0].is_file())

    def test_chatterbox_validates_reference_before_model_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.wav"
            reference.write_bytes(b"reference")
            messages: list[str] = []
            synthesizer = ChatterboxTTSSynthesizer(
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated(),
                logger=messages.append,
            )

            fake_librosa = SimpleNamespace(get_duration=lambda **_values: 8.0)
            with mock.patch.dict("sys.modules", {"librosa": fake_librosa}):
                synthesizer._validate_reference()

            self.assertEqual(messages, [])

    def test_chatterbox_rejects_unreadable_or_implausible_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.wav"
            reference.write_bytes(b"reference")
            synthesizer = ChatterboxTTSSynthesizer(
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated()
            )

            cases = (
                (lambda **_values: float("nan"), "keine nutzbare"),
                (lambda **_values: 0.1, "keine nutzbare"),
                (lambda **_values: 61.0, "länger als 60"),
            )
            for get_duration, expected_message in cases:
                with self.subTest(expected_message=expected_message):
                    fake_librosa = SimpleNamespace(get_duration=get_duration)
                    with (
                        mock.patch.dict("sys.modules", {"librosa": fake_librosa}),
                        self.assertRaisesRegex(SpeechSynthesisError, expected_message),
                    ):
                        synthesizer._validate_reference()

            def fail_to_read(**_values):
                raise RuntimeError("invalid audio")

            fake_librosa = SimpleNamespace(get_duration=fail_to_read)
            with (
                mock.patch.dict("sys.modules", {"librosa": fake_librosa}),
                self.assertRaisesRegex(SpeechSynthesisError, "nicht als Audio lesbar"),
            ):
                synthesizer._validate_reference()

    def test_chatterbox_warns_for_suboptimal_reference_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.wav"
            reference.write_bytes(b"reference")
            messages: list[str] = []
            synthesizer = ChatterboxTTSSynthesizer(
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated(),
                logger=messages.append,
            )
            fake_librosa = SimpleNamespace(get_duration=lambda **_values: 2.0)

            with mock.patch.dict("sys.modules", {"librosa": fake_librosa}):
                synthesizer._validate_reference()

            self.assertEqual(len(messages), 1)
            self.assertIn("2.0 Sekunden", messages[0])

    def test_local_synthesis_honors_cancellation_before_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "voice.onnx"
            model.write_bytes(b"model")
            Path(f"{model}.json").write_text("{}", encoding="utf-8")
            synthesizer = PiperTTSSynthesizer(
                AppConfig(tts_engine="piper", piper_model=model).validated()
            )
            synthesizer._piper = _FakePiper()
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(CancelledError):
                synthesizer.synthesize(
                    (Segment(0, 1, "Hallo"),),
                    root / "out",
                    cancel_event=cancelled,
                )
            self.assertEqual(_FakePiper.loaded, 0)

    def test_piper_cancellation_after_generator_yield_is_not_masked_by_wav_close(self) -> None:
        cancelled = threading.Event()

        class CancellingVoice:
            def synthesize(self, _text, *, syn_config):
                cancelled.set()
                yield _PiperChunk()

        class CancellingPiper(_FakePiper):
            class PiperVoice:
                @staticmethod
                def load(**_values):
                    return CancellingVoice()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "voice.onnx"
            model.write_bytes(b"model")
            Path(f"{model}.json").write_text("{}", encoding="utf-8")
            synthesizer = PiperTTSSynthesizer(
                AppConfig(tts_engine="piper", tts_device="cpu", piper_model=model).validated()
            )
            synthesizer._piper = CancellingPiper()
            with self.assertRaises(CancelledError):
                synthesizer.synthesize(
                    (Segment(0, 1, "Hallo"),),
                    root / "out",
                    cancel_event=cancelled,
                )
            self.assertFalse(any((root / "out").glob("*.wav")))


if __name__ == "__main__":
    unittest.main()
