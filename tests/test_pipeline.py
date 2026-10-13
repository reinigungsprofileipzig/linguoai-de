import tempfile
import unittest
from pathlib import Path
from unittest import mock

from linguoai.config import AppConfig
from linguoai.domain import AudioChunk, Segment, TranscriptionResult
from linguoai.errors import ConfigurationError, FileOperationError
from linguoai.media import NormalizationResult
from linguoai.pipeline import VideoDubbingPipeline


class FakeMedia:
    def extract_audio_chunks(self, source, work, **kwargs):
        chunk = work / "chunk.wav"
        chunk.write_bytes(b"wave")
        callback = kwargs.get("on_chunk")
        if callback:
            callback(1, 1)
        return [AudioChunk(0, chunk, 0.0, 2.0, 0.0, 2.0)], 2.0

    def normalize_voice_clip(self, source, destination, **kwargs):
        destination.write_bytes(b"normalized")
        duration = kwargs["slot_duration"]
        return NormalizationResult(duration, duration, 1.0, 1.0)

    def build_timeline(self, segments, clips, destination, **kwargs):
        destination.write_bytes(b"timeline")

    def mux_video(self, source, audio, output, **kwargs):
        output.write_bytes(b"valid-video")

    def cancel_all(self):
        return


class FakeTranscriber:
    def transcribe(self, chunks, **kwargs):
        callback = kwargs.get("on_progress")
        if callback:
            callback(1, 1)
        return TranscriptionResult((Segment(0.25, 1.75, "Hello world"),), "en", 0.99)


class FakeTranslator:
    def translate_segments(self, segments, **kwargs):
        callback = kwargs.get("on_progress")
        if callback:
            callback(1, 1)
        return [segment.with_text("Hallo Welt", remember_original=True) for segment in segments]

    def close(self):
        return


class FakeSynthesizer:
    def synthesize(self, segments, output_directory, **kwargs):
        output_directory.mkdir(parents=True)
        clip = output_directory / "voice.mp3"
        clip.write_bytes(b"voice")
        callback = kwargs.get("on_progress")
        if callback:
            callback(1, 1)
        return [clip]


class PipelineTests(unittest.TestCase):
    def test_chatterbox_releases_whisper_and_nllb_before_model_load(self) -> None:
        class Closable:
            def __init__(self) -> None:
                self.closed = 0

            def close(self) -> None:
                self.closed += 1

        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "reference.wav"
            reference.write_bytes(b"reference")
            transcriber = Closable()
            translator = Closable()
            pipeline = VideoDubbingPipeline(
                AppConfig(
                    target_language="de",
                    translation_service="nllb",
                    tts_engine="chatterbox",
                    tts_reference_audio=reference,
                    confirm_voice_rights=True,
                ),
                media=FakeMedia(),
                transcriber=transcriber,
                translator=translator,
                synthesizer=FakeSynthesizer(),
            )

            pipeline._release_upstream_models_for_tts()

            self.assertEqual(transcriber.closed, 1)
            self.assertEqual(translator.closed, 1)

    def test_end_to_end_contract_without_external_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            result = pipeline.run_path(source, output)
            self.assertEqual(len(result.completed), 1)
            self.assertEqual(output.read_bytes(), b"valid-video")
            self.assertIn("Hello world", result.completed[0].original_subtitles.read_text("utf-8"))
            self.assertIn("Hallo Welt", result.completed[0].translated_subtitles.read_text("utf-8"))

    def test_overwrite_publish_rolls_back_the_whole_result_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_target = root / "first.srt"
            second_target = root / "second.mp4"
            first_target.write_bytes(b"old-first")
            second_target.write_bytes(b"old-second")
            first_partial = root / "first.partial"
            missing_partial = root / "missing.partial"
            first_partial.write_bytes(b"new-first")
            pipeline = VideoDubbingPipeline(
                AppConfig(overwrite=True),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            with self.assertRaises(FileOperationError):
                pipeline._publish_artifacts(
                    (
                        (first_partial, first_target),
                        (missing_partial, second_target),
                    )
                )
            self.assertEqual(first_target.read_bytes(), b"old-first")
            self.assertEqual(second_target.read_bytes(), b"old-second")

    def test_output_reservation_blocks_concurrent_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output.mp4"
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            lock, token = pipeline._reserve_output(output)
            try:
                with self.assertRaises(ConfigurationError):
                    pipeline._reserve_output(output)
            finally:
                pipeline._release_output(lock, token)
            self.assertFalse(lock.exists())

    def test_output_reservation_covers_shared_subtitles_across_video_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "output.mp4"
            second = root / "output.mkv"
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            lock, token = pipeline._reserve_output(first)
            try:
                with self.assertRaises(ConfigurationError):
                    pipeline._reserve_output(second)
            finally:
                pipeline._release_output(lock, token)

    def test_overwrite_rejects_directory_artifact_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.mp4"
            output = root / "movie.mp4"
            source.write_bytes(b"source")
            subtitle_directory = root / "movie.original.srt"
            subtitle_directory.mkdir()
            pipeline = VideoDubbingPipeline(
                AppConfig(overwrite=True),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            with self.assertRaisesRegex(ConfigurationError, "Verzeichnisse"):
                pipeline.run_path(source, output)
            self.assertTrue(subtitle_directory.is_dir())

    def test_publish_rechecks_directory_created_during_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "result.partial"
            target = root / "result.mp4"
            partial.write_bytes(b"new")
            target.mkdir()
            pipeline = VideoDubbingPipeline(
                AppConfig(overwrite=True),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            with self.assertRaises(ConfigurationError):
                pipeline._publish_artifacts(((partial, target),))
            self.assertTrue(target.is_dir())

    def test_keyboard_interrupt_after_link_rolls_back_owned_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "result.partial"
            target = root / "result.mp4"
            partial.write_bytes(b"new")
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            real_link = __import__("os").link

            def link_then_interrupt(source, destination):
                real_link(source, destination)
                raise KeyboardInterrupt

            with (
                mock.patch("linguoai.pipeline.os.link", side_effect=link_then_interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                pipeline._publish_artifacts(((partial, target),))
            self.assertFalse(target.exists())

    def test_interrupt_while_closing_fallback_placeholder_removes_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "result.partial"
            target = root / "result.mp4"
            partial.write_bytes(b"new")
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            real_close = __import__("os").close
            calls = 0

            def close_then_interrupt(descriptor):
                nonlocal calls
                calls += 1
                real_close(descriptor)
                if calls == 1:
                    raise KeyboardInterrupt

            with (
                mock.patch("linguoai.pipeline.os.link", side_effect=OSError("no hardlink")),
                mock.patch("linguoai.pipeline.os.close", side_effect=close_then_interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                pipeline._publish_artifacts(((partial, target),))
            self.assertFalse(target.exists())

    def test_keyboard_interrupt_after_backup_restores_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "result.partial"
            target = root / "result.mp4"
            partial.write_bytes(b"new")
            target.write_bytes(b"old")
            pipeline = VideoDubbingPipeline(
                AppConfig(overwrite=True),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            real_replace = __import__("os").replace
            calls = 0

            def replace_then_interrupt(source, destination):
                nonlocal calls
                calls += 1
                real_replace(source, destination)
                if calls == 1:
                    raise KeyboardInterrupt

            with (
                mock.patch(
                    "linguoai.pipeline.os.replace",
                    side_effect=replace_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                pipeline._publish_artifacts(((partial, target),))
            self.assertEqual(target.read_bytes(), b"old")

    def test_same_source_and_target_folder_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.mp4").write_bytes(b"source")
            pipeline = VideoDubbingPipeline(
                AppConfig(overwrite=True),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            with self.assertRaisesRegex(ConfigurationError, "Quell- und Zielordner"):
                pipeline.run_path(root, root)

    def test_folder_output_names_are_unique_after_extension_disambiguation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            for name in ("demo.mp4", "demo.mkv", "demo-mp4.mp4"):
                (source / name).write_bytes(name.encode())
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            result = pipeline.run_path(source, target)
            names = [item.output_path.name.casefold() for item in result.completed]
            self.assertEqual(len(names), 3)
            self.assertEqual(len(set(names)), 3)

    def test_direct_output_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.mp4"
            valuable = root / "valuable.mp4"
            output = root / "chosen.mp4"
            source.write_bytes(b"source")
            valuable.write_bytes(b"keep")
            try:
                output.symlink_to(valuable)
            except OSError as exc:
                self.skipTest(f"Symlinks sind nicht verfügbar: {exc}")
            pipeline = VideoDubbingPipeline(
                AppConfig(overwrite=True),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            with self.assertRaisesRegex(ConfigurationError, "symbolische Links"):
                pipeline.run_path(source, output)
            self.assertEqual(valuable.read_bytes(), b"keep")

    def test_folder_files_with_same_stem_get_distinct_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            (source / "demo.mp4").write_bytes(b"one")
            (source / "demo.mkv").write_bytes(b"two")
            pipeline = VideoDubbingPipeline(
                AppConfig(),
                media=FakeMedia(),
                transcriber=FakeTranscriber(),
                translator=FakeTranslator(),
                synthesizer=FakeSynthesizer(),
            )
            result = pipeline.run_path(source, target)
            self.assertEqual(len(result.completed), 2)
            self.assertEqual(
                {item.output_path.name for item in result.completed},
                {"demo-mkv.translated-en.mp4", "demo-mp4.translated-en.mp4"},
            )


if __name__ == "__main__":
    unittest.main()
