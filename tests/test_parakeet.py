import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from linguoai.cli import main
from linguoai.config import AppConfig
from linguoai.domain import AudioChunk, BatchResult
from linguoai.errors import CancelledError, ConfigurationError, DependencyError, TranscriptionError
from linguoai.parakeet import ParakeetTranscriber
from linguoai.transcription import FasterWhisperTranscriber, create_transcriber


class FakeRecognizer:
    def __init__(self, results, cancel_event=None):
        self.results = iter(results)
        self.cancel_event = cancel_event
        self.samples = []
        self.decoded = 0

    def create_stream(self):
        def accept(sample_rate, samples):
            self.samples.append((sample_rate, samples))

        return SimpleNamespace(accept_waveform=accept)

    def decode_stream(self, stream):
        self.decoded += 1
        stream.result = next(self.results)
        if self.cancel_event:
            self.cancel_event.set()


def result(tokens, timestamps):
    return SimpleNamespace(text="".join(tokens), tokens=tokens, timestamps=timestamps)


def model_files(directory):
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (directory / name).write_bytes(b"placeholder")


def wav(path, *, sample_rate=16000):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x40" * sample_rate)


class ParakeetTests(unittest.TestCase):
    def test_whisper_remains_default_and_parakeet_factory_is_optional(self):
        self.assertIsInstance(create_transcriber(AppConfig()), FasterWhisperTranscriber)
        self.assertIsInstance(
            create_transcriber(AppConfig(asr_engine="parakeet")), ParakeetTranscriber
        )

    def test_language_and_model_selection_validate_before_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            options = {"asr_engine": "parakeet", "parakeet_model_directory": directory}
            with self.assertRaisesRegex(ConfigurationError, "Quellsprache"):
                AppConfig(**options).validated()
            with self.assertRaisesRegex(ConfigurationError, "unterstützt"):
                AppConfig(**options, source_language="ja").validated()
            with self.assertRaisesRegex(ConfigurationError, "unterstützt"):
                AppConfig(
                    **options, source_language="en", parakeet_profile="german-primeline"
                ).validated()
            config = AppConfig(
                **options, source_language="de-DE", parakeet_profile="german-primeline"
            ).validated()
            self.assertEqual(config.parakeet_threads, 4)
            self.assertEqual(config.validate_parakeet_language("de-DE"), "de")
            AppConfig(**options, source_language="hr").validated()

    def test_complete_onnx_export_required_and_no_implicit_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            transcriber = ParakeetTranscriber(
                AppConfig(
                    asr_engine="parakeet",
                    source_language="de",
                    parakeet_model_directory=directory,
                )
            )
            with self.assertRaisesRegex(ConfigurationError, "encoder"):
                transcriber.preflight()
            model_files(directory)
            (directory / "tokens.txt").unlink()
            with self.assertRaisesRegex(ConfigurationError, "Tokenliste"):
                transcriber.preflight()

    def test_missing_optional_runtime_has_actionable_install_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model_files(directory)
            transcriber = ParakeetTranscriber(
                AppConfig(
                    asr_engine="parakeet",
                    source_language="de",
                    parakeet_model_directory=directory,
                )
            )
            with (
                mock.patch.dict(sys.modules, {"sherpa_onnx": None}),
                self.assertRaisesRegex(DependencyError, r"\[parakeet\]"),
            ):
                transcriber.preflight()

    def test_fake_sherpa_loads_cpu_four_threads_and_pcm_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model_files(directory)
            audio_path = directory / "chunk.wav"
            wav(audio_path)
            recognizer = FakeRecognizer([result([" Hallo", " Welt", "."], [0.0, 0.3, 0.4])])
            factory = mock.Mock(return_value=recognizer)
            fake_sherpa = SimpleNamespace(
                OfflineRecognizer=SimpleNamespace(from_transducer=factory)
            )
            config = AppConfig(
                asr_engine="parakeet", source_language="de", parakeet_model_directory=directory
            ).validated()
            progress = mock.Mock()
            with mock.patch.dict(sys.modules, {"sherpa_onnx": fake_sherpa}):
                transcriber = ParakeetTranscriber(config)
                outcome = transcriber.transcribe(
                    [AudioChunk(0, audio_path, 10, 11, 10, 11)],
                    source_language="de",
                    cancel_event=threading.Event(),
                    on_progress=progress,
                )
            self.assertEqual(factory.call_args.kwargs["provider"], "cpu")
            self.assertEqual(factory.call_args.kwargs["num_threads"], 4)
            self.assertEqual(factory.call_args.kwargs["model_type"], "nemo_transducer")
            self.assertTrue(factory.call_args.kwargs["encoder"].endswith("encoder.int8.onnx"))
            self.assertEqual(recognizer.samples[0][0], 16000)
            self.assertAlmostEqual(float(recognizer.samples[0][1][0]), 0.5)
            self.assertEqual(outcome.language, "de")
            self.assertEqual(outcome.segments[0].text, "Hallo Welt.")
            self.assertEqual(outcome.segments[0].start, 10)
            self.assertLessEqual(outcome.segments[0].end, 11)
            progress.assert_called_once_with(1, 1)
            transcriber.close()
            self.assertIsNone(transcriber._recognizer)

    def test_word_ownership_prevents_whole_sentence_loss_in_chunk_overlap(self):
        chunk = AudioChunk(1, Path("unused.wav"), 18, 42, 20, 40)
        outcome = ParakeetTranscriber._segments(
            result([" duplicate", " keep", " this", ".", " next"], [0.0, 2.0, 3.0, 3.0, 22.0]),
            chunk,
        )
        self.assertEqual(" ".join(segment.text for segment in outcome), "keep this.")
        self.assertEqual(outcome[0].start, 20)
        self.assertLess(outcome[0].end, 40)

    def test_empty_text_can_be_skipped_but_missing_or_invalid_timing_fails(self):
        chunk = AudioChunk(0, Path("unused.wav"), 0, 10, 0, 10)
        self.assertEqual(ParakeetTranscriber._segments(result([], []), chunk), ())
        for bad in (
            result([" Hello"], []),
            result([" Hello"], [float("nan")]),
            result([" Hello", " world"], [1, 0]),
        ):
            with self.subTest(bad=bad), self.assertRaises(TranscriptionError):
                ParakeetTranscriber._segments(bad, chunk)

    def test_cancellation_after_native_decode_stops_before_next_chunk(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio_path = Path(temporary) / "chunk.wav"
            wav(audio_path)
            cancel = threading.Event()
            recognizer = FakeRecognizer([result([" Hello"], [0])], cancel)
            transcriber = ParakeetTranscriber(
                AppConfig(asr_engine="parakeet", source_language="en"), recognizer=recognizer
            )
            chunks = [
                AudioChunk(index, audio_path, index, index + 1, index, index + 1)
                for index in range(2)
            ]
            with self.assertRaises(CancelledError):
                transcriber.transcribe(chunks, source_language="en", cancel_event=cancel)
            self.assertEqual(recognizer.decoded, 1)

    def test_malformed_audio_returns_chunk_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio_path = Path(temporary) / "chunk.wav"
            wav(audio_path, sample_rate=8000)
            recognizer = FakeRecognizer([])
            transcriber = ParakeetTranscriber(
                AppConfig(asr_engine="parakeet", source_language="de"), recognizer=recognizer
            )
            with self.assertRaisesRegex(TranscriptionError, "Chunk 1.*16 kHz"):
                transcriber.transcribe(
                    [AudioChunk(0, audio_path, 0, 1, 0, 1)],
                    source_language="de",
                    cancel_event=threading.Event(),
                )

    def test_cli_options_reach_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            pipeline = mock.Mock()
            pipeline.run_path.return_value = BatchResult()
            with mock.patch("linguoai.cli.VideoDubbingPipeline", return_value=pipeline) as factory:
                exit_code = main(
                    [
                        "-i",
                        "input.mp4",
                        "-o",
                        "output.mp4",
                        "--asr-engine",
                        "parakeet",
                        "--source-language",
                        "de",
                        "--parakeet-model-directory",
                        temporary,
                        "--parakeet-profile",
                        "german-primeline",
                        "--parakeet-threads",
                        "4",
                        "--quiet",
                    ]
                )
            self.assertEqual(exit_code, 0)
            config = factory.call_args.args[0].validated()
            self.assertEqual(config.asr_engine, "parakeet")
            self.assertEqual(config.parakeet_profile, "german-primeline")


if __name__ == "__main__":
    unittest.main()
