import unittest

from linguoai.domain import Segment
from linguoai.subtitles import format_srt_timestamp, render_srt


class SubtitleTests(unittest.TestCase):
    def test_timestamp_rounds_across_second_boundary(self) -> None:
        self.assertEqual(format_srt_timestamp(59.9996), "00:01:00,000")

    def test_render_srt(self) -> None:
        rendered = render_srt([Segment(0.0, 1.25, "Hallo")])
        self.assertIn("00:00:00,000 --> 00:00:01,250", rendered)
        self.assertIn("Hallo", rendered)


if __name__ == "__main__":
    unittest.main()
