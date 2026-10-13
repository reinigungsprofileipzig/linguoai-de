import unittest

from linguoai.domain import Segment
from linguoai.timing import (
    expand_into_free_gaps,
    merge_overfull_segments,
    rebalance_neighboring_windows,
    required_speeds,
)


class TimingTests(unittest.TestCase):
    def test_free_pause_is_borrowed_without_overlap(self) -> None:
        segments = [Segment(1.0, 3.0, "A"), Segment(4.0, 6.0, "B")]
        result = expand_into_free_gaps(segments, [3.0, 1.0], media_duration=7.0)
        self.assertTrue(result.changed)
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertLessEqual(result.segments[0].end, result.segments[1].start)

    def test_neighbor_windows_are_rebalanced_toward_longer_audio(self) -> None:
        segments = [Segment(0.0, 2.0, "A"), Segment(2.0, 5.0, "B")]
        result = rebalance_neighboring_windows(
            segments,
            [3.5, 1.5],
            preferred_speed=1.35,
        )
        self.assertTrue(result.changed)
        self.assertGreater(result.segments[0].duration, 2.0)
        self.assertEqual(result.segments[0].end, result.segments[1].start)

    def test_overfull_neighbor_is_merged(self) -> None:
        segments = [Segment(0.0, 1.0, "A"), Segment(1.0, 4.0, "B")]
        result = merge_overfull_segments(
            segments,
            [2.1, 1.0],
            preferred_speed=1.35,
        )
        self.assertTrue(result.changed)
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].text, "A B")

    def test_required_speed_uses_adjusted_slot(self) -> None:
        speeds = required_speeds([Segment(0.0, 2.0, "A")], [3.0])
        self.assertEqual(speeds, [1.5])

    def test_unmergeable_cue_does_not_block_other_overfull_neighbors(self) -> None:
        segments = [
            Segment(0.0, 1.0, "X" * 421),
            Segment(10.0, 11.0, "A"),
            Segment(11.0, 14.0, "B"),
        ]
        result = merge_overfull_segments(
            segments,
            [5.0, 2.1, 1.0],
            preferred_speed=1.35,
        )
        self.assertEqual(result.merge_count, 1)
        self.assertEqual(result.segments[0], segments[0])
        self.assertEqual(result.segments[1].text, "A B")


if __name__ == "__main__":
    unittest.main()
