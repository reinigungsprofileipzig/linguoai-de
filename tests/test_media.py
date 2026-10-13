import tempfile
import threading
import unittest
import wave
from pathlib import Path

from linguoai.domain import Segment
from linguoai.media import MediaTools, atempo_chain


def write_pcm(path: Path, seconds: float) -> None:
    frames = round(seconds * MediaTools.SAMPLE_RATE)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(MediaTools.CHANNELS)
        output.setsampwidth(MediaTools.SAMPLE_WIDTH)
        output.setframerate(MediaTools.SAMPLE_RATE)
        output.writeframes(b"\x00" * frames * MediaTools.SAMPLE_WIDTH)


class MediaTests(unittest.TestCase):
    def test_atempo_chain_keeps_each_factor_supported(self) -> None:
        factors = atempo_chain(6.0)
        product = 1.0
        for factor in factors:
            self.assertGreaterEqual(factor, 0.5)
            self.assertLessEqual(factor, 2.0)
            product *= factor
        self.assertAlmostEqual(product, 6.0)

    def test_timeline_contains_leading_and_trailing_silence(self) -> None:
        media = MediaTools(ffmpeg="unused", ffprobe="unused")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clip = root / "clip.wav"
            output = root / "timeline.wav"
            write_pcm(clip, 0.5)
            media.build_timeline(
                [Segment(0.5, 1.0, "Hallo")],
                [clip],
                output,
                media_duration=2.0,
                cancel_event=threading.Event(),
            )
            with wave.open(str(output), "rb") as reader:
                self.assertEqual(reader.getframerate(), MediaTools.SAMPLE_RATE)
                self.assertEqual(reader.getnframes(), 2 * MediaTools.SAMPLE_RATE)

    def test_exact_chunk_multiple_does_not_create_empty_chunk(self) -> None:
        class RecordingRunner:
            def __init__(self) -> None:
                self.commands = []

            def run(self, command, **kwargs):
                self.commands.append(command)
                write_pcm(Path(command[-1]), 0.01)
                return "", ""

            def cancel_all(self):
                return

        class FixedDurationMedia(MediaTools):
            def probe_duration(self, path, cancel_event):
                return 240.0

        runner = RecordingRunner()
        media = FixedDurationMedia(runner=runner, ffmpeg="unused", ffprobe="unused")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks, duration = media.extract_audio_chunks(
                root / "video.mp4",
                root,
                chunk_seconds=120.0,
                overlap_seconds=1.0,
                cancel_event=threading.Event(),
            )
        self.assertEqual(duration, 240.0)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(runner.commands), 2)


if __name__ == "__main__":
    unittest.main()
