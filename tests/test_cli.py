import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from linguoai.cli import build_parser, main
from linguoai.config import AppConfig
from linguoai.domain import BatchResult, JobFailure


class FakePipeline:
    last_config = None

    def __init__(self, config, *, event_sink):
        type(self).last_config = config.validated()

    def run_path(self, source, output):
        return BatchResult()

    def cancel(self):
        return


class CliTests(unittest.TestCase):
    def test_retry_default_matches_shared_config(self) -> None:
        arguments = build_parser().parse_args([])
        self.assertEqual(arguments.request_retries, AppConfig().request_retries)

    def test_optimize_uses_ui_independent_config(self) -> None:
        with (
            mock.patch("linguoai.cli.VideoDubbingPipeline", FakePipeline),
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False),
        ):
            exit_code = main(
                [
                    "-i",
                    "input.mp4",
                    "-o",
                    "output.mp4",
                    "--optimize",
                    "--quiet",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertTrue(FakePipeline.last_config.optimize_transcript)
        self.assertFalse(hasattr(FakePipeline.last_config, "enable_optimization_var"))

    def test_batch_failure_returns_exit_code_one(self) -> None:
        class FailedPipeline(FakePipeline):
            def run_path(self, source, output):
                return BatchResult(failed=(JobFailure(Path(source), "kaputt"),))

        with mock.patch("linguoai.cli.VideoDubbingPipeline", FailedPipeline):
            exit_code = main(["-i", "input.mp4", "-o", "output.mp4", "--quiet"])
        self.assertEqual(exit_code, 1)

    def test_local_nllb_options_reach_shared_config(self) -> None:
        with mock.patch("linguoai.cli.VideoDubbingPipeline", FakePipeline):
            exit_code = main(
                [
                    "-i",
                    "input.mp4",
                    "-o",
                    "output.mp4",
                    "--service",
                    "nllb",
                    "--nllb-device",
                    "cpu",
                    "--nllb-model",
                    "my/model",
                    "--nllb-target-code",
                    "deu_Latn",
                    "--quiet",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(FakePipeline.last_config.translation_service, "nllb")
        self.assertEqual(FakePipeline.last_config.nllb_device, "cpu")
        self.assertEqual(FakePipeline.last_config.nllb_model, "my/model")

    def test_piper_options_reach_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "voice.onnx"
            model_config = root / "voice.onnx.json"
            model.write_bytes(b"onnx")
            model_config.write_text("{}", encoding="utf-8")
            with mock.patch("linguoai.cli.VideoDubbingPipeline", FakePipeline):
                exit_code = main(
                    [
                        "-i",
                        "input.mp4",
                        "-o",
                        "output.mp4",
                        "--tts-engine",
                        "piper",
                        "--tts-device",
                        "cpu",
                        "--piper-model",
                        str(model),
                        "--piper-config",
                        str(model_config),
                        "--piper-speaker-id",
                        "2",
                        "--piper-length-scale",
                        "1.2",
                        "--quiet",
                    ]
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(FakePipeline.last_config.tts_engine, "piper")
        self.assertEqual(FakePipeline.last_config.tts_device, "cpu")
        self.assertEqual(FakePipeline.last_config.piper_model, model)
        self.assertEqual(FakePipeline.last_config.piper_config, model_config)
        self.assertEqual(FakePipeline.last_config.piper_speaker_id, 2)
        self.assertEqual(FakePipeline.last_config.piper_length_scale, 1.2)

    def test_chatterbox_reference_and_consent_reach_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.wav"
            reference.write_bytes(b"wave")
            with mock.patch("linguoai.cli.VideoDubbingPipeline", FakePipeline):
                exit_code = main(
                    [
                        "-i",
                        "input.mp4",
                        "-o",
                        "output.mp4",
                        "--tts-engine",
                        "chatterbox",
                        "--tts-device",
                        "cuda",
                        "--tts-reference-audio",
                        str(reference),
                        "--confirm-voice-rights",
                        "--chatterbox-model",
                        "v2",
                        "--chatterbox-exaggeration",
                        "0.7",
                        "--chatterbox-cfg-weight",
                        "0.35",
                        "--quiet",
                    ]
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(FakePipeline.last_config.tts_engine, "chatterbox")
        self.assertEqual(FakePipeline.last_config.tts_device, "cuda")
        self.assertEqual(FakePipeline.last_config.tts_reference_audio, reference)
        self.assertTrue(FakePipeline.last_config.confirm_voice_rights)
        self.assertEqual(FakePipeline.last_config.chatterbox_model, "v2")
        self.assertEqual(FakePipeline.last_config.chatterbox_exaggeration, 0.7)
        self.assertEqual(FakePipeline.last_config.chatterbox_cfg_weight, 0.35)


if __name__ == "__main__":
    unittest.main()
