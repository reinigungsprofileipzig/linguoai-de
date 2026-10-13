import threading
import unittest
from types import SimpleNamespace

from linguoai.config import LANGUAGES
from linguoai.errors import TranslationError
from linguoai.local_translation import NLLB_LANGUAGE_MAP, NllbTranslationAdapter


class LocalTranslationTests(unittest.TestCase):
    def test_all_ui_languages_have_a_flores_mapping(self) -> None:
        self.assertEqual(set(LANGUAGES) - set(NLLB_LANGUAGE_MAP), set())

    def test_custom_flores_code_supports_additional_languages(self) -> None:
        code = NllbTranslationAdapter._language_code(
            None,
            role="Zielsprache",
            override="ace_Latn",
        )
        self.assertEqual(code, "ace_Latn")

    def test_known_target_rejects_conflicting_flores_override(self) -> None:
        adapter = NllbTranslationAdapter(
            model_name="test",
            target_language="de",
            target_code_override="ace_Latn",
        )
        with self.assertRaisesRegex(TranslationError, "widerspricht"):
            adapter._target_language_code("de")

    def test_requested_cuda_is_checked_before_model_download(self) -> None:
        adapter = NllbTranslationAdapter(model_name="test", device="cuda")
        adapter._torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
        )
        adapter._transformers = object()
        with self.assertRaisesRegex(TranslationError, "CUDA"):
            adapter.preflight()

    def test_unknown_source_flores_code_is_rejected_by_tokenizer(self) -> None:
        adapter = NllbTranslationAdapter(model_name="test")
        adapter._tokenizer = SimpleNamespace(
            unk_token_id=0,
            convert_tokens_to_ids=lambda _code: 0,
        )
        with self.assertRaisesRegex(TranslationError, "Quellcode"):
            adapter._set_source_language("abc_Latn")

    def test_invalid_flores_code_is_rejected_before_model_weights_load(self) -> None:
        weight_loads: list[str] = []

        class FakeTokenizerFactory:
            @staticmethod
            def from_pretrained(*args, **kwargs):
                return SimpleNamespace(
                    unk_token_id=0,
                    convert_tokens_to_ids=lambda code: 0 if code == "abc_Latn" else 1,
                )

        class FakeModelFactory:
            @staticmethod
            def from_pretrained(*args, **kwargs):
                weight_loads.append("loaded")
                return object()

        adapter = NllbTranslationAdapter(model_name="test", device="cpu")
        adapter._torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
        )
        adapter._transformers = SimpleNamespace(
            AutoTokenizer=FakeTokenizerFactory,
            AutoModelForSeq2SeqLM=FakeModelFactory,
        )
        with self.assertRaisesRegex(TranslationError, "Quellcode"):
            adapter._ensure_model("abc_Latn", "deu_Latn", threading.Event())
        self.assertEqual(weight_loads, [])

    def test_internal_batches_preserve_order(self) -> None:
        class LightweightAdapter(NllbTranslationAdapter):
            def _ensure_model(
                self,
                source_code: str,
                target_code: str,
                cancel_event: threading.Event,
            ) -> None:
                return

            def _set_source_language(self, source_code: str) -> None:
                return

            def _translate_group(
                self,
                texts: list[str],
                target_code: str,
                cancel_event: threading.Event,
            ) -> list[str]:
                self.groups.append(tuple(texts))
                return [f"translated:{text}" for text in texts]

        adapter = LightweightAdapter(model_name="test", batch_size=2)
        adapter.groups = []
        result = adapter.translate_batch(
            ["eins", "zwei", "drei"],
            source_language="de",
            target_language="en",
        )
        self.assertEqual(adapter.groups, [("eins", "zwei"), ("drei",)])
        self.assertEqual(
            result,
            ["translated:eins", "translated:zwei", "translated:drei"],
        )

    def test_regional_language_falls_back_to_known_base_code(self) -> None:
        self.assertEqual(
            NllbTranslationAdapter._language_code("en-US", role="Quellsprache"),
            "eng_Latn",
        )
        self.assertEqual(
            NllbTranslationAdapter._language_code("zh-TW", role="Quellsprache"),
            "zho_Hant",
        )
        self.assertEqual(
            NllbTranslationAdapter._language_code("zh-HK", role="Quellsprache"),
            "zho_Hant",
        )

    def test_eos_at_decoder_start_does_not_hide_truncation(self) -> None:
        self.assertFalse(NllbTranslationAdapter._sequence_has_generated_eos([2, 17, 18], 2))
        self.assertTrue(NllbTranslationAdapter._sequence_has_generated_eos([2, 17, 2], 2))

    def test_placeholder_fallback_reinserts_marker_exactly(self) -> None:
        class FragmentAdapter(NllbTranslationAdapter):
            def _translate_group(
                self,
                texts: list[str],
                target_code: str,
                cancel_event: threading.Event,
            ) -> list[str]:
                return [f"übersetzt({text})" for text in texts]

        marker = f"__LINGUO_{'A' * 32}_0000__"
        adapter = FragmentAdapter(model_name="test")
        rebuilt = adapter._translate_around_placeholders(
            f"Visit {marker} now.",
            "deu_Latn",
            threading.Event(),
        )
        self.assertEqual(rebuilt.count(marker), 1)
        self.assertIn("übersetzt(Visit)", rebuilt)
        self.assertIn("übersetzt(now.)", rebuilt)


if __name__ == "__main__":
    unittest.main()
