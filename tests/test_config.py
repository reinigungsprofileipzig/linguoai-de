import math
import tempfile
import unittest
from pathlib import Path

from linguoai.config import AppConfig
from linguoai.errors import ConfigurationError


class AppConfigTests(unittest.TestCase):
    def test_default_configuration_is_valid(self) -> None:
        config = AppConfig()
        self.assertIs(config.validated(), config)

    def test_deepl_requires_key(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DEEPL_API_KEY"):
            AppConfig(translation_service="deepl").validated()

    def test_optimizer_requires_gemini_key(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "GEMINI_API_KEY"):
            AppConfig(optimize_transcript=True).validated()

    def test_primary_provider_cannot_be_hidden_fallback(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Primärdienst"):
            AppConfig(fallback_services=("google",)).validated()

    def test_secrets_are_not_exposed_by_repr(self) -> None:
        config = AppConfig(gemini_api_key="super-secret", deepl_api_key="also-secret")
        rendered = repr(config)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("also-secret", rendered)

    def test_language_aliases_and_auto_are_normalized(self) -> None:
        config = AppConfig(target_language="ZH-cn", source_language="AUTO")
        self.assertEqual(config.target_language, "zh-CN")
        self.assertIsNone(config.source_language)

    def test_nllb_needs_no_cloud_key(self) -> None:
        config = AppConfig(translation_service="NLLB")
        self.assertIs(config.validated(), config)

    def test_non_finite_numeric_values_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                AppConfig(request_timeout_seconds=value).validated()

    def test_invalid_flores_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "NLLB-Zielcode"):
            AppConfig(
                translation_service="nllb",
                nllb_target_code="german",
            ).validated()

    def test_piper_requires_model_and_matching_configuration(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "onnx"):
            AppConfig(tts_engine="piper").validated()
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "voice.onnx"
            model.write_bytes(b"model")
            with self.assertRaisesRegex(ConfigurationError, "Modellkonfiguration"):
                AppConfig(tts_engine="piper", piper_model=model).validated()
            Path(f"{model}.json").write_text("{}", encoding="utf-8")
            config = AppConfig(
                tts_engine="PIPER",
                piper_model=model,
                piper_speaker_id=0,
            )
            self.assertIs(config.validated(), config)

    def test_chatterbox_requires_reference_rights_and_supported_language(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "voice.wav"
            reference.write_bytes(b"voice")
            with self.assertRaisesRegex(ConfigurationError, "Erlaubnis"):
                AppConfig(
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                ).validated()
            with self.assertRaisesRegex(ConfigurationError, "unterstützt"):
                AppConfig(
                    target_language="vi",
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ).validated()
            with self.assertRaisesRegex(ConfigurationError, "Multilingual-V2"):
                AppConfig(
                    target_language="de",
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                    chatterbox_model="v3",
                ).validated()
            config = AppConfig(
                target_language="zh-CN",
                tts_engine="chatterbox",
                tts_reference_audio=reference,
                confirm_voice_rights=True,
            )
            self.assertIs(config.validated(), config)

    def test_tts_numeric_controls_must_be_finite_and_bounded(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Längenfaktor"):
            AppConfig(piper_length_scale=math.nan).validated()
        with self.assertRaisesRegex(ConfigurationError, "CFG"):
            AppConfig(chatterbox_cfg_weight=1.1).validated()


if __name__ == "__main__":
    unittest.main()
