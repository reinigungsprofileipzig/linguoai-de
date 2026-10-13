from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("ttkbootstrap"), "ttkbootstrap not installed")
class GuiHelperTests(unittest.TestCase):
    def test_language_display_and_custom_code_are_parsed(self) -> None:
        from linguoai.gui import LinguoAIApp

        self.assertEqual(LinguoAIApp._code_from_display("Deutsch [de]"), "de")
        self.assertEqual(LinguoAIApp._code_from_display("zh-cn"), "zh-CN")
        self.assertEqual(LinguoAIApp._code_from_display("ace"), "ace")

    def test_file_output_suggestion_contains_current_target(self) -> None:
        from linguoai.gui import LinguoAIApp

        source = Path("C:/Videos/demo.mp4")
        suggestion = LinguoAIApp._suggest_output_path(source, "fr", "file")
        self.assertEqual(Path(suggestion), source.with_name("demo.translated-fr.mp4"))

    def test_folder_output_suggestion_contains_current_target(self) -> None:
        from linguoai.gui import LinguoAIApp

        source = Path("C:/Videos/Eingang")
        suggestion = LinguoAIApp._suggest_output_path(source, "de", "folder")
        self.assertEqual(Path(suggestion), source.with_name("Eingang-translated-de"))

    def test_chatterbox_language_check_handles_regional_codes(self) -> None:
        from linguoai.gui import LinguoAIApp

        self.assertTrue(LinguoAIApp._chatterbox_supports_language("de-DE"))
        self.assertTrue(LinguoAIApp._chatterbox_supports_language("zh-CN"))
        self.assertFalse(LinguoAIApp._chatterbox_supports_language("vi"))


if __name__ == "__main__":
    unittest.main()
