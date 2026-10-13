import unittest

from linguoai.domain import Segment
from linguoai.errors import TranslationError
from linguoai.text import (
    batches_by_size,
    coalesce_segments,
    deduplicate_overlapping_segments,
    protect_entities,
    restore_entities,
    sanitize_segments,
)


class TextTests(unittest.TestCase):
    def test_entities_round_trip(self) -> None:
        source = "Mail an test@example.com, öffne https://example.com und zahle 12,50 €."
        protected = protect_entities(source)
        self.assertNotIn("test@example.com", protected.text)
        self.assertNotIn("12,50 €", protected.text)
        self.assertEqual(restore_entities(protected.text, protected.entities), source)

    def test_missing_entity_is_rejected(self) -> None:
        protected = protect_entities("Öffne https://example.com")
        with self.assertRaises(TranslationError):
            restore_entities("Öffne die Seite", protected.entities)

    def test_existing_old_style_placeholder_does_not_collide(self) -> None:
        source = "__LINGUO_ENTITY_0000__ und test@example.com"
        protected = protect_entities(source)
        self.assertIn("__LINGUO_ENTITY_0000__", protected.text)
        self.assertEqual(restore_entities(protected.text, protected.entities), source)

    def test_duplicate_entity_tokens_are_rejected(self) -> None:
        protected = protect_entities("a@example.com und a@example.com")
        first, second = (item[0] for item in protected.entities)
        corrupted = protected.text.replace(second, first)
        with self.assertRaises(TranslationError):
            restore_entities(corrupted, protected.entities)

    def test_url_does_not_absorb_sentence_punctuation(self) -> None:
        protected = protect_entities("Siehe https://example.com/path.")
        self.assertEqual(protected.entities[0][1], "https://example.com/path")

    def test_balanced_parentheses_remain_part_of_url(self) -> None:
        url = "https://en.wikipedia.org/wiki/Function_(mathematics)"
        protected = protect_entities(f"Siehe {url}.")
        self.assertEqual(protected.entities[0][1], url)
        self.assertEqual(restore_entities(protected.text, protected.entities), f"Siehe {url}.")

    def test_unbalanced_sentence_parenthesis_is_not_part_of_url(self) -> None:
        protected = protect_entities("(Siehe https://example.com/path).")
        self.assertEqual(protected.entities[0][1], "https://example.com/path")

    def test_prefix_currency_is_protected(self) -> None:
        protected = protect_entities("Das kostet $12.50.")
        self.assertEqual(protected.entities[0][1], "$12.50")

    def test_only_overlapping_duplicate_is_removed(self) -> None:
        segments = [
            Segment(0.0, 2.0, "Guten Morgen"),
            Segment(1.5, 2.5, "Guten Morgen"),
            Segment(4.0, 5.0, "Guten Morgen"),
        ]
        result = deduplicate_overlapping_segments(segments)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1].start, 4.0)

    def test_sanitize_makes_timeline_monotonic(self) -> None:
        segments = [Segment(0.0, 2.0, "A"), Segment(1.5, 3.0, "B")]
        result = sanitize_segments(segments, media_duration=10.0)
        self.assertEqual(result[1].start, 2.0)

    def test_coalesce_preserves_outer_timestamps(self) -> None:
        result = coalesce_segments([Segment(0.0, 2.0, "Hallo"), Segment(2.1, 4.0, "Welt")])
        self.assertEqual(result, [Segment(0.0, 4.0, "Hallo Welt")])

    def test_batches_respect_hard_limits(self) -> None:
        batches = batches_by_size(["a" * 4, "b" * 4, "c" * 4], maximum_characters=8)
        self.assertEqual(batches, [(0, 2), (2, 3)])


if __name__ == "__main__":
    unittest.main()
