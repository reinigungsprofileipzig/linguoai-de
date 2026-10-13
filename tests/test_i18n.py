from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from linguoai.i18n import (
    CATALOGS,
    DEFAULT_UI_LOCALE,
    SUPPORTED_UI_LOCALES,
    Translator,
    catalog_placeholder_errors,
    detect_ui_locale,
    get_user_config_path,
    load_saved_ui_locale,
    normalize_ui_locale,
    placeholder_names,
    save_ui_locale,
)


class LocaleTests(unittest.TestCase):
    def test_supported_locales_are_stable_and_complete(self) -> None:
        self.assertEqual(SUPPORTED_UI_LOCALES, ("de", "en", "ru", "uk", "tr", "es", "it"))
        self.assertEqual(set(CATALOGS), set(SUPPORTED_UI_LOCALES))

    def test_normalize_accepts_regions_encodings_and_language_names(self) -> None:
        cases = {
            "de-DE": "de",
            "en_US.UTF-8": "en",
            "ru_RU": "ru",
            "uk-UA": "uk",
            "tr_TR@latin": "tr",
            "Spanish_Spain.1252": "es",
            "Italian": "it",
            "TÜRKISCH": "tr",
        }
        for candidate, expected in cases.items():
            with self.subTest(candidate=candidate):
                self.assertEqual(normalize_ui_locale(candidate), expected)

    def test_normalize_uses_supported_fallback_then_english(self) -> None:
        self.assertEqual(normalize_ui_locale("xx", fallback="uk-UA"), "uk")
        self.assertEqual(normalize_ui_locale("xx", fallback="yy"), DEFAULT_UI_LOCALE)
        self.assertEqual(normalize_ui_locale(None), DEFAULT_UI_LOCALE)

    def test_detection_priority_is_explicit_environment_os_then_english(self) -> None:
        environment = {"LINGUOAI_UI_LANGUAGE": "tr-TR"}
        self.assertEqual(
            detect_ui_locale("ru-RU", environ=environment, os_locale="es-ES"),
            "ru",
        )
        self.assertEqual(
            detect_ui_locale(None, environ=environment, os_locale="es-ES"),
            "tr",
        )
        self.assertEqual(
            detect_ui_locale(None, environ={}, os_locale="es-ES"),
            "es",
        )
        self.assertEqual(
            detect_ui_locale("invalid", environ={}, os_locale="C"),
            DEFAULT_UI_LOCALE,
        )

    def test_invalid_higher_priority_candidate_falls_through(self) -> None:
        self.assertEqual(
            detect_ui_locale(
                "auto",
                environ={"LINGUOAI_UI_LANGUAGE": "unsupported"},
                os_locale="uk_UA.UTF-8",
            ),
            "uk",
        )

    def test_system_locale_is_used_when_no_injected_value_exists(self) -> None:
        with mock.patch("linguoai.i18n._system_locale_name", return_value="it_IT"):
            self.assertEqual(detect_ui_locale(environ={}), "it")


class TranslatorTests(unittest.TestCase):
    def test_translator_is_immutable_and_normalizes_its_locale(self) -> None:
        translator = Translator("uk-UA")
        self.assertEqual(translator.locale, "uk")
        with self.assertRaises(FrozenInstanceError):
            translator.locale = "de"  # type: ignore[misc]

    def test_known_strings_are_translated_in_every_locale(self) -> None:
        expected = {
            "de": "Verarbeitung starten",
            "en": "Start processing",
            "ru": "Начать обработку",
            "uk": "Почати обробку",
            "tr": "İşlemeyi başlat",
            "es": "Iniciar procesamiento",
            "it": "Avvia elaborazione",
        }
        for locale_code, translation in expected.items():
            with self.subTest(locale=locale_code):
                self.assertEqual(
                    Translator(locale_code).gettext("Verarbeitung starten"),
                    translation,
                )

    def test_named_parameters_are_formatted(self) -> None:
        translated = Translator("es").gettext(
            "Fehlgeschlagen: {path}: {error}",
            path="video.mp4",
            error="sin audio",
        )
        self.assertEqual(translated, "Falló: video.mp4: sin audio")

    def test_missing_parameter_never_raises_or_partially_formats(self) -> None:
        translated = Translator("ru").gettext(
            "Fehlgeschlagen: {path}: {error}",
            path="video.mp4",
        )
        self.assertEqual(translated, "Ошибка: {path}: {error}")

    def test_unknown_string_falls_back_to_german_source(self) -> None:
        source = "Noch nicht im Katalog"
        self.assertEqual(Translator("ru").gettext(source), source)

    def test_unsupported_locale_falls_back_to_english(self) -> None:
        self.assertEqual(Translator("fr").gettext("Abbrechen"), "Cancel")

    def test_call_alias_matches_gettext(self) -> None:
        translator = Translator("tr")
        self.assertEqual(translator("Fehler"), translator.gettext("Fehler"))

    def test_catalogs_are_immutable(self) -> None:
        with self.assertRaises(TypeError):
            CATALOGS["en"]["Start"] = "Changed"  # type: ignore[index]

    def test_catalog_keys_and_placeholders_are_complete(self) -> None:
        german_keys = set(CATALOGS["de"])
        for locale_code in SUPPORTED_UI_LOCALES:
            with self.subTest(locale=locale_code):
                self.assertEqual(set(CATALOGS[locale_code]), german_keys)
        self.assertEqual(catalog_placeholder_errors(), ())

    def test_placeholder_extraction_handles_format_specs(self) -> None:
        self.assertEqual(
            placeholder_names("{name}: {percent:.1f}% ({name})"),
            frozenset({"name", "percent"}),
        )
        with self.assertRaises(ValueError):
            placeholder_names("{broken")


class PersistenceTests(unittest.TestCase):
    def test_custom_config_root_is_respected(self) -> None:
        path = get_user_config_path(
            environ={"LINGUOAI_CONFIG_HOME": "~/custom-linguoai"},
            home=Path("C:/ignored"),
        )
        self.assertEqual(path.name, "settings.json")
        self.assertEqual(path.parent.name, "custom-linguoai")

    def test_save_and_load_are_atomic_and_preserve_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"theme": "dark", "ui_language": "de"}),
                encoding="utf-8",
            )

            self.assertTrue(save_ui_locale("uk-UA", path))
            self.assertEqual(load_saved_ui_locale(path), "uk")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["theme"], "dark")
            self.assertEqual(saved["ui_language"], "uk")
            self.assertEqual(saved["version"], 1)
            self.assertEqual(list(path.parent.glob(".settings.json.*.tmp")), [])

    def test_invalid_locale_does_not_replace_existing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            original = '{"ui_language": "de"}\n'
            path.write_text(original, encoding="utf-8")
            self.assertFalse(save_ui_locale("unsupported", path))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_malformed_missing_or_oversized_settings_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.json"
            self.assertIsNone(load_saved_ui_locale(missing))

            malformed = root / "malformed.json"
            malformed.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_saved_ui_locale(malformed))

            unsupported = root / "unsupported.json"
            unsupported.write_text('{"ui_language": "fr"}', encoding="utf-8")
            self.assertIsNone(load_saved_ui_locale(unsupported))

            oversized = root / "oversized.json"
            oversized.write_text(" " * (65 * 1024), encoding="utf-8")
            self.assertIsNone(load_saved_ui_locale(oversized))

    def test_failed_replace_keeps_old_file_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            original = '{"ui_language": "de"}\n'
            path.write_text(original, encoding="utf-8")
            with mock.patch("linguoai.i18n.os.replace", side_effect=OSError("locked")):
                self.assertFalse(save_ui_locale("es", path))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob(".settings.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
