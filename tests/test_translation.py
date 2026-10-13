import threading
import unittest
from unittest import mock

from linguoai.config import AppConfig
from linguoai.domain import Segment
from linguoai.errors import CancelledError, TranslationError
from linguoai.translation import (
    GOOGLE_TARGET_LANGUAGE_ALIASES,
    DeepTranslatorAdapter,
    GeminiStructuredClient,
    TranslationManager,
)


class FakeAdapter:
    def __init__(self, name: str, *, error: Exception | None = None, prefix: str = "") -> None:
        self.name = name
        self.error = error
        self.prefix = prefix
        self.calls = 0

    def translate_batch(self, texts, *, source_language, target_language, cancel_event=None):
        self.calls += 1
        if self.error:
            raise self.error
        return [self.prefix + text for text in texts]

    def close(self) -> None:
        return


class ContextAwareFakeAdapter(FakeAdapter):
    supports_context = True

    def __init__(self, name: str, *, prefix: str = "") -> None:
        super().__init__(name, prefix=prefix)
        self.context = None

    def translate_batch(
        self, texts, *, source_language, target_language, cancel_event=None, context=None
    ):
        self.context = context
        return super().translate_batch(
            texts,
            source_language=source_language,
            target_language=target_language,
            cancel_event=cancel_event,
        )


class TranslationTests(unittest.TestCase):
    def test_only_selected_provider_is_called(self) -> None:
        google = FakeAdapter("google", prefix="DE: ")
        hidden = FakeAdapter("gemini", prefix="G: ")
        manager = TranslationManager(
            AppConfig(request_retries=1),
            adapters={"google": google, "gemini": hidden},
        )
        result = manager.translate_segments(
            [Segment(0.0, 1.0, "Hello")],
            source_language="en",
            cancel_event=threading.Event(),
        )
        self.assertEqual(result[0].text, "DE: Hello")
        self.assertEqual(google.calls, 1)
        self.assertEqual(hidden.calls, 0)

    def test_explicit_fallback_is_used_after_failure(self) -> None:
        primary = FakeAdapter("google", error=RuntimeError("offline"))
        fallback = FakeAdapter("gemini", prefix="DE: ")
        config = AppConfig(
            fallback_services=("gemini",),
            gemini_api_key="test-key",
            request_retries=1,
        )
        manager = TranslationManager(
            config,
            adapters={"google": primary, "gemini": fallback},
        )
        result = manager.translate_segments(
            [Segment(0.0, 1.0, "Hello")],
            source_language="en",
            cancel_event=threading.Event(),
        )
        self.assertEqual(result[0].text, "DE: Hello")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_provider_errors_redact_api_keys(self) -> None:
        secret = "very-secret-key"
        adapter = FakeAdapter(
            "deepl",
            error=RuntimeError(f"request failed: auth_key={secret}"),
        )
        logs: list[str] = []
        manager = TranslationManager(
            AppConfig(
                translation_service="deepl",
                deepl_api_key=secret,
                request_retries=1,
            ),
            adapters={"deepl": adapter},
            logger=logs.append,
        )
        with self.assertRaises(TranslationError) as caught:
            manager.translate_segments(
                [Segment(0.0, 1.0, "private cue")],
                source_language="en",
                cancel_event=threading.Event(),
            )
        combined = f"{caught.exception} {' '.join(logs)}"
        self.assertNotIn(secret, combined)
        self.assertIn("[REDACTED]", combined)

    def test_deepl_retries_server_errors(self) -> None:
        error = RuntimeError("server")
        error.http_status_code = 502
        self.assertTrue(DeepTranslatorAdapter("deepl").is_retryable(error))

    def test_gemini_retries_connect_errors(self) -> None:
        ConnectError = type("ConnectError", (RuntimeError,), {})
        self.assertTrue(GeminiStructuredClient.is_retryable(ConnectError()))

    def test_google_uses_legacy_hebrew_target_code(self) -> None:
        self.assertEqual(GOOGLE_TARGET_LANGUAGE_ALIASES["he"], "iw")

    def test_full_transcript_is_passed_as_context_when_supported(self) -> None:
        adapter = ContextAwareFakeAdapter("gemini", prefix="DE: ")
        manager = TranslationManager(
            AppConfig(
                translation_service="gemini",
                gemini_api_key="test-key",
                request_retries=1,
            ),
            adapters={"gemini": adapter},
        )
        manager.translate_segments(
            [Segment(0.0, 1.0, "First"), Segment(1.0, 2.0, "Second")],
            source_language="en",
            cancel_event=threading.Event(),
        )
        self.assertIn("First", adapter.context)
        self.assertIn("Second", adapter.context)

    def test_context_including_truncation_marker_respects_configured_limit(self) -> None:
        manager = TranslationManager(
            AppConfig(translation_context_characters=1001),
            adapters={"google": FakeAdapter("google")},
        )
        context = manager._translation_context(["start " + "word " * 500 + "end"])
        self.assertEqual(len(context), 1001)
        self.assertTrue(context.startswith("[00001] start"))
        self.assertTrue(context.endswith("end"))
        self.assertIn("Kontext gekürzt", context)

    def test_context_changes_invalidate_cache_but_identical_context_reuses_it(self) -> None:
        adapter = ContextAwareFakeAdapter("gemini", prefix="DE: ")
        manager = TranslationManager(
            AppConfig(
                translation_service="gemini",
                gemini_api_key="test-key",
                request_retries=1,
            ),
            adapters={"gemini": adapter},
        )
        for second_text in ("Context one", "Context two", "Context two"):
            manager.translate_segments(
                [Segment(0.0, 1.0, "Same cue"), Segment(1.0, 2.0, second_text)],
                source_language="en",
                cancel_event=threading.Event(),
            )
        self.assertEqual(adapter.calls, 2)

    def test_provider_retries_then_recovers_without_using_fallback(self) -> None:
        primary = FakeAdapter("google")
        fallback = FakeAdapter("gemini")
        manager = TranslationManager(
            AppConfig(
                fallback_services=("gemini",),
                gemini_api_key="test-key",
                request_retries=2,
                request_retry_base_seconds=0,
            ),
            adapters={"google": primary, "gemini": fallback},
        )
        with mock.patch.object(
            primary, "translate_batch", side_effect=[RuntimeError("temporary"), ["Hallo"]]
        ) as request:
            result = manager.translate_segments(
                [Segment(0.0, 1.0, "Hello")],
                source_language="en",
                cancel_event=threading.Event(),
            )
        self.assertEqual(result[0].text, "Hallo")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(fallback.calls, 0)

    def test_cancelled_provider_is_never_retried_or_sent_to_fallback(self) -> None:
        primary = FakeAdapter("google", error=CancelledError("cancelled"))
        fallback = FakeAdapter("gemini")
        manager = TranslationManager(
            AppConfig(fallback_services=("gemini",), gemini_api_key="test-key"),
            adapters={"google": primary, "gemini": fallback},
        )
        with self.assertRaises(CancelledError):
            manager.translate_segments(
                [Segment(0.0, 1.0, "Hello")],
                source_language="en",
                cancel_event=threading.Event(),
            )
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)


if __name__ == "__main__":
    unittest.main()
