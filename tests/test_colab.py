import ast
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from linguoai.colab_ui import (
    _batch_sources,
    _copy_file_cancellable,
    _start_cloudflare_tunnel,
    _stop_active_tunnel,
    _validated_tunnel_hostname,
    _wait_for_cloudflare_tunnel,
    launch_colab,
    process_colab_batch,
    process_colab_job,
    resolve_drive_output_directory,
    resolve_input_video,
    resolve_optional_job_file,
)
from linguoai.domain import BatchResult, JobResult
from linguoai.errors import CancelledError


class ColabTests(unittest.TestCase):
    def setUp(self) -> None:
        printing = mock.patch("builtins.print")
        printing.start()
        self.addCleanup(printing.stop)

    def tearDown(self) -> None:
        _stop_active_tunnel()

    def test_cloudflare_hostname_must_be_a_plain_https_url(self) -> None:
        self.assertEqual(
            _validated_tunnel_hostname("https://APP.LinguoAI.de/"),
            "https://app.linguoai.de",
        )
        for invalid in (
            "",
            "http://app.linguoai.de",
            "https://app.linguoai.de/path",
            "https://app.linguoai.de:443",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _validated_tunnel_hostname(invalid)

    def test_named_cloudflare_tunnel_uses_environment_token_not_command_line(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch(
                "linguoai.colab_ui._resolve_cloudflared_command",
                return_value="/usr/local/bin/cloudflared",
            ),
            mock.patch("linguoai.colab_ui.subprocess.Popen", return_value=process) as popen,
            mock.patch("linguoai.colab_ui._wait_for_cloudflare_tunnel") as ready,
        ):
            public_url = _start_cloudflare_tunnel("secret-token", "https://app.linguoai.de")

        self.assertEqual(public_url, "https://app.linguoai.de")
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/local/bin/cloudflared", "tunnel", "--metrics"])
        self.assertTrue(command[3].startswith("127.0.0.1:"))
        self.assertEqual(command[4:], ["run"])
        self.assertNotIn("secret-token", command)
        self.assertEqual(popen.call_args.kwargs["env"]["TUNNEL_TOKEN"], "secret-token")
        ready.assert_called_once_with(process, f"http://{command[3]}/ready", timeout=30.0)

    def test_tunnel_is_cleaned_up_when_readiness_is_interrupted(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch(
                "linguoai.colab_ui._resolve_cloudflared_command", return_value="cloudflared"
            ),
            mock.patch("linguoai.colab_ui.subprocess.Popen", return_value=process),
            mock.patch(
                "linguoai.colab_ui._wait_for_cloudflare_tunnel", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            _start_cloudflare_tunnel("secret-token", "https://app.example.com")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_tunnel_is_cleaned_up_when_readiness_fails(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch(
                "linguoai.colab_ui._resolve_cloudflared_command", return_value="cloudflared"
            ),
            mock.patch("linguoai.colab_ui.subprocess.Popen", return_value=process),
            mock.patch(
                "linguoai.colab_ui._wait_for_cloudflare_tunnel",
                side_effect=RuntimeError("Startfrist"),
            ),
            self.assertRaisesRegex(RuntimeError, "Startfrist"),
        ):
            _start_cloudflare_tunnel("secret-token", "https://app.example.com")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_stubborn_tunnel_is_killed_and_reaped(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("cloudflared", 3), 0]
        with mock.patch("linguoai.colab_ui._active_tunnel_process", process):
            _stop_active_tunnel()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_readiness_waits_until_cloudflared_returns_200(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        opener = mock.Mock()
        opener.open.side_effect = [OSError("not ready"), response]
        with (
            mock.patch("linguoai.colab_ui.build_opener", return_value=opener),
            mock.patch("linguoai.colab_ui.time.sleep"),
        ):
            _wait_for_cloudflare_tunnel(process, "http://127.0.0.1:1234/ready", timeout=3)
        self.assertEqual(opener.open.call_count, 2)

    def test_readiness_fails_when_process_exits(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "beendet"):
            _wait_for_cloudflare_tunnel(process, "http://127.0.0.1:1234/ready", timeout=3)

    def test_readiness_respects_deadline(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch("linguoai.colab_ui.time.monotonic", side_effect=[0.0, 4.0]),
            self.assertRaisesRegex(RuntimeError, "Startfrist"),
        ):
            _wait_for_cloudflare_tunnel(process, "http://127.0.0.1:1234/ready", timeout=3)

    def test_launch_colab_uses_local_gradio_server_and_named_tunnel(self) -> None:
        class FakeDemo:
            launch_kwargs: dict[str, object]
            closed = False

            def launch(self, **kwargs: object) -> None:
                self.launch_kwargs = kwargs

            def block_thread(self) -> None:
                raise KeyboardInterrupt

            def close(self) -> None:
                self.closed = True

        demo = FakeDemo()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("linguoai.colab_ui.OUTPUT_ROOT", root / "outputs"),
                mock.patch("linguoai.colab_ui.JOB_ROOT", root / "jobs"),
                mock.patch("linguoai.colab_ui.DRIVE_ROOT", root / "drive"),
                mock.patch("linguoai.colab_ui.build_demo", return_value=demo),
                mock.patch(
                    "linguoai.colab_ui._start_cloudflare_tunnel",
                    return_value="https://app.linguoai.de",
                ) as start_tunnel,
                self.assertRaises(KeyboardInterrupt),
            ):
                launch_colab(
                    share_provider="cloudflare",
                    tunnel_token="secret-token",
                    tunnel_hostname="https://app.linguoai.de",
                )

        self.assertFalse(demo.launch_kwargs["share"])
        self.assertEqual(demo.launch_kwargs["server_name"], "127.0.0.1")
        self.assertEqual(demo.launch_kwargs["server_port"], 7860)
        self.assertTrue(demo.launch_kwargs["prevent_thread_lock"])
        self.assertEqual(demo.launch_kwargs["max_file_size"], 95_000_000)
        start_tunnel.assert_called_once_with(
            "secret-token",
            "https://app.linguoai.de",
            command="cloudflared",
            startup_timeout=30.0,
        )
        self.assertTrue(demo.closed)

    def test_launch_defaults_to_authenticated_gradio_link_without_cloudflare(self) -> None:
        demo = mock.Mock(share_url="https://example.gradio.live")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("linguoai.colab_ui.OUTPUT_ROOT", root / "outputs"),
                mock.patch("linguoai.colab_ui.JOB_ROOT", root / "jobs"),
                mock.patch("linguoai.colab_ui.DRIVE_ROOT", root / "drive"),
                mock.patch("linguoai.colab_ui.build_demo", return_value=demo) as build,
                mock.patch("linguoai.colab_ui._start_cloudflare_tunnel") as start_tunnel,
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                launch_colab()
        build.assert_called_once_with(upload_limit_mb=5000, share_provider="gradio")
        kwargs = demo.launch.call_args.kwargs
        self.assertTrue(kwargs["share"])
        self.assertEqual(kwargs["server_name"], "127.0.0.1")
        self.assertEqual(kwargs["max_file_size"], 5_000_000_000)
        self.assertEqual(kwargs["auth"][0], "linguoai")
        self.assertTrue(kwargs["auth"][1])
        self.assertEqual(kwargs["blocked_paths"], [str(root / "drive")])
        start_tunnel.assert_not_called()
        demo.block_thread.assert_called_once()
        demo.close.assert_called_once()

    def test_launch_failure_closes_gradio_and_cleans_tunnel(self) -> None:
        for provider in ("gradio", "cloudflare"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                demo = mock.Mock()
                demo.launch.side_effect = RuntimeError("launch failed")
                with (
                    mock.patch("linguoai.colab_ui.OUTPUT_ROOT", root / "outputs"),
                    mock.patch("linguoai.colab_ui.JOB_ROOT", root / "jobs"),
                    mock.patch("linguoai.colab_ui.build_demo", return_value=demo),
                    mock.patch("linguoai.colab_ui._stop_active_tunnel") as stop,
                    mock.patch("linguoai.colab_ui._start_cloudflare_tunnel") as start,
                    self.assertRaisesRegex(RuntimeError, "launch failed"),
                ):
                    launch_colab(
                        share_provider=provider,
                        tunnel_token="test-token",
                        tunnel_hostname="https://app.example.com",
                    )
                start.assert_not_called()
                self.assertEqual(stop.call_count, 2)
                demo.close.assert_called_once()

    def test_missing_gradio_share_link_reports_failure_and_closes_server(self) -> None:
        demo = mock.Mock(share_url=None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("linguoai.colab_ui.OUTPUT_ROOT", root / "outputs"),
                mock.patch("linguoai.colab_ui.JOB_ROOT", root / "jobs"),
                mock.patch("linguoai.colab_ui.build_demo", return_value=demo),
                self.assertRaisesRegex(RuntimeError, "keinen öffentlichen Link"),
            ):
                launch_colab()
        demo.block_thread.assert_not_called()
        demo.close.assert_called_once()

    def test_invalid_share_options_fail_before_building_ui(self) -> None:
        options = [
            {"share_provider": "quick"},
            {"upload_limit_mb": 0},
            {"upload_limit_mb": True},
            {"upload_limit_mb": 1.5},
            {"server_port": 65536},
            {"share_provider": "cloudflare", "tunnel_startup_timeout": float("nan")},
            {"share_provider": "cloudflare", "tunnel_hostname": ""},
        ]
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("linguoai.colab_ui.build_demo") as build,
        ):
            for kwargs in options:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    launch_colab(**kwargs)
        build.assert_not_called()

    def test_cloudflare_requires_secret_before_building_ui(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("linguoai.colab_ui.build_demo") as build,
            self.assertRaisesRegex(RuntimeError, "TOKEN fehlt"),
        ):
            launch_colab(share_provider="cloudflare", tunnel_hostname="https://app.example.com")
        build.assert_not_called()

    @staticmethod
    def batch_options() -> dict[str, object]:
        return {
            "uploaded_videos": ["first.mp4", "second.mp4"],
            "drive_paths": "",
            "target_language": "de",
            "source_language": "auto",
            "translation_service": "google",
            "whisper_model": "tiny",
            "device": "cpu",
            "nllb_device": "cpu",
            "nllb_model": "facebook/nllb-200-distilled-600M",
            "nllb_revision": "",
            "nllb_batch_size": 1,
            "nllb_source_code": "",
            "nllb_target_code": "",
            "tts_voice": "",
            "deepl_api_key": "",
            "gemini_api_key": "",
            "optimize_transcript": False,
            "save_to_drive": False,
            "drive_output_folder": "",
        }

    def test_batch_uploads_take_precedence_and_drive_paths_preserve_order(self) -> None:
        self.assertEqual(
            _batch_sources(["first.mp4", "second.mp4"], "ignored.mp4"),
            [("first.mp4", "", "first.mp4"), ("second.mp4", "", "second.mp4")],
        )
        self.assertEqual(
            _batch_sources(None, "Videos/first.mp4\n\n Videos/second.mp4 "),
            [(None, "Videos/first.mp4", "first.mp4"), (None, "Videos/second.mp4", "second.mp4")],
        )
        with self.assertRaises(ValueError):
            _batch_sources([], " ")

    def test_batch_processes_in_order_and_aggregates_downloads(self) -> None:
        progress = mock.Mock()
        with mock.patch(
            "linguoai.colab_ui.process_colab_job",
            side_effect=[("first ready", ["first-out.mp4"]), ("second ready", ["second-out.mp4"])],
        ) as job:
            status, files = process_colab_batch(**self.batch_options(), progress_callback=progress)
        self.assertIn("2/2", status)
        self.assertEqual(files, ["first-out.mp4", "second-out.mp4"])
        self.assertEqual([call.args[0] for call in job.call_args_list], ["first.mp4", "second.mp4"])
        progress.assert_called_with(1.0, "Batch abgeschlossen.")

    def test_batch_preserves_successful_downloads_after_a_failed_job(self) -> None:
        with mock.patch(
            "linguoai.colab_ui.process_colab_job",
            side_effect=[RuntimeError("invalid video"), ("second ready", ["second-out.mp4"])],
        ):
            status, files = process_colab_batch(**self.batch_options())
        self.assertIn("1/2", status)
        self.assertIn("invalid video", status)
        self.assertEqual(files, ["second-out.mp4"])

    def test_batch_can_stop_on_first_error(self) -> None:
        with (
            mock.patch(
                "linguoai.colab_ui.process_colab_job", side_effect=RuntimeError("invalid video")
            ) as job,
            self.assertRaisesRegex(RuntimeError, "invalid video"),
        ):
            process_colab_batch(**self.batch_options(), continue_after_error=False)
        self.assertEqual(job.call_count, 1)

    def test_batch_does_not_swallow_cancellation(self) -> None:
        with (
            mock.patch(
                "linguoai.colab_ui.process_colab_job", side_effect=CancelledError("cancelled")
            ) as job,
            self.assertRaises(CancelledError),
        ):
            process_colab_batch(**self.batch_options())
        self.assertEqual(job.call_count, 1)

    def test_job_preserves_cancellation_exception(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            options = self.batch_options()
            options.pop("uploaded_videos")
            options.pop("drive_paths")
            with (
                mock.patch("linguoai.colab_ui.COLAB_ROOT", root),
                mock.patch("linguoai.colab_ui.JOB_ROOT", root / "jobs"),
                mock.patch("linguoai.colab_ui.OUTPUT_ROOT", root / "outputs"),
                mock.patch(
                    "linguoai.colab_ui.shutil.disk_usage",
                    return_value=SimpleNamespace(free=10 * 1024**3),
                ),
                self.assertRaises(CancelledError),
            ):
                process_colab_job(str(video), "", **options, cancel_event=cancel_event)

    def test_drive_input_is_confined_to_my_drive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MyDrive"
            video = root / "Videos" / "demo.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            self.assertEqual(
                resolve_input_video(None, "Videos/demo.mp4", drive_root=root),
                video.resolve(),
            )
            with self.assertRaisesRegex(ValueError, "MyDrive"):
                resolve_input_video(None, "../secret.mp4", drive_root=root)

    def test_upload_takes_precedence_over_drive_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "upload.mkv"
            video.write_bytes(b"video")
            self.assertEqual(
                resolve_input_video(str(video), "../ignored.mp4"),
                video.resolve(),
            )

    def test_drive_output_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MyDrive"
            root.mkdir()
            with self.assertRaises(ValueError):
                resolve_drive_output_directory("../outside", drive_root=root)

    def test_optional_tts_file_is_confined_to_my_drive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MyDrive"
            reference = root / "Voices" / "reference.wav"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(b"wave")
            self.assertEqual(
                resolve_optional_job_file(
                    None,
                    "Voices/reference.wav",
                    label="Stimmreferenz",
                    allowed_extensions=frozenset({".wav"}),
                    required=True,
                    drive_root=root,
                ),
                reference.resolve(),
            )
            with self.assertRaisesRegex(ValueError, "MyDrive"):
                resolve_optional_job_file(
                    None,
                    "../reference.wav",
                    label="Stimmreferenz",
                    allowed_extensions=frozenset({".wav"}),
                    required=True,
                    drive_root=root,
                )

    def test_optional_tts_upload_takes_precedence_and_checks_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            uploaded = Path(temporary) / "voice.onnx"
            uploaded.write_bytes(b"onnx")
            self.assertEqual(
                resolve_optional_job_file(
                    str(uploaded),
                    "../ignored.onnx",
                    label="Piper-Modell",
                    allowed_extensions=frozenset({".onnx"}),
                    required=True,
                ),
                uploaded.resolve(),
            )
            with self.assertRaisesRegex(ValueError, "Dateiformat"):
                resolve_optional_job_file(
                    str(uploaded),
                    "",
                    label="JSON",
                    allowed_extensions=frozenset({".json"}),
                )

    def test_piper_assets_are_copied_into_isolated_job(self) -> None:
        class FakeColabPipeline:
            checked_job_directory = False
            last_config = None

            def __init__(self, config, *, event_sink, cancel_event):
                type(self).last_config = config.validated()

            def run_path(self, source, output):
                config = type(self).last_config
                type(self).checked_job_directory = (
                    config.piper_model.parent == Path(source).parent
                    and config.piper_config.parent == Path(source).parent
                    and config.piper_model.name == "piper-model.onnx"
                    and config.piper_config.name == "piper-model.onnx.json"
                )
                output.write_bytes(b"video")
                original = output.with_suffix(".original.srt")
                translated = output.with_suffix(".translated.srt")
                original.write_text("original", encoding="utf-8")
                translated.write_text("translated", encoding="utf-8")
                completed = JobResult(
                    input_path=Path(source),
                    output_path=Path(output),
                    original_subtitles=original,
                    translated_subtitles=translated,
                    source_language="en",
                    segment_count=1,
                )
                return BatchResult(completed=(completed,))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            model = root / "voice.onnx"
            model_config = root / "voice.onnx.json"
            video.write_bytes(b"video")
            model.write_bytes(b"onnx")
            model_config.write_text("{}", encoding="utf-8")
            with (
                mock.patch("linguoai.colab_ui.COLAB_ROOT", root),
                mock.patch("linguoai.colab_ui.JOB_ROOT", root / "jobs"),
                mock.patch("linguoai.colab_ui.OUTPUT_ROOT", root / "outputs"),
                mock.patch("linguoai.colab_ui.VideoDubbingPipeline", FakeColabPipeline),
                mock.patch("linguoai.colab_ui._schedule_output_cleanup"),
                mock.patch(
                    "linguoai.colab_ui.shutil.disk_usage",
                    return_value=SimpleNamespace(free=10 * 1024**3),
                ),
            ):
                status, downloads = process_colab_job(
                    uploaded_video=str(video),
                    drive_path="",
                    target_language="de",
                    source_language="auto",
                    translation_service="google",
                    whisper_model="tiny",
                    device="cpu",
                    nllb_device="cpu",
                    nllb_model="facebook/nllb-200-distilled-600M",
                    nllb_revision="",
                    nllb_batch_size=1,
                    nllb_source_code="",
                    nllb_target_code="",
                    tts_voice="",
                    deepl_api_key="",
                    gemini_api_key="",
                    optimize_transcript=False,
                    save_to_drive=False,
                    drive_output_folder="",
                    tts_engine="piper",
                    tts_device="cpu",
                    uploaded_piper_model=str(model),
                    uploaded_piper_config=str(model_config),
                )
            self.assertIn("Fertig mit piper", status)
            self.assertEqual(len(downloads), 3)
            self.assertTrue(FakeColabPipeline.checked_job_directory)

    def test_notebook_is_valid_and_has_no_stored_outputs(self) -> None:
        path = Path(__file__).resolve().parents[1] / "notebooks" / "LinguoAI_Colab.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(notebook["nbformat"], 4)
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                ast.parse("".join(cell["source"]))
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])
        sources = "\n".join("".join(cell["source"]) for cell in notebook["cells"])
        self.assertIn("LOCAL_TTS_EXTRA", sources)

    def test_large_file_copy_honors_cancellation_and_removes_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            destination = root / "destination.mp4"
            source.write_bytes(b"video")
            cancel_event = threading.Event()
            cancel_event.set()
            with self.assertRaises(CancelledError):
                _copy_file_cancellable(source, destination, cancel_event)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
