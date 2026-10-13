"""Moderne Tk-Oberfläche mit strikt thread-sicherer Pipeline-Anbindung."""

from __future__ import annotations

import importlib.util
import os
import queue
import shutil
import threading
import tkinter as tk
import traceback
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

import ttkbootstrap as ttk
from ttkbootstrap.scrolled import ScrolledFrame

from .config import (
    CHATTERBOX_LANGUAGES,
    LANGUAGES,
    VIDEO_EXTENSIONS,
    AppConfig,
    normalize_language_code,
)
from .domain import BatchResult, ProgressUpdate, Stage
from .errors import CancelledError, ConfigurationError, DependencyError, LinguoAIError
from .pipeline import VideoDubbingPipeline
from .runtime import redact_secrets
from .speech import DEFAULT_EDGE_VOICES
from .translation import DEEPL_TARGET_LANGUAGE_MAP

SERVICE_DISPLAY = {
    "NLLB lokal · CPU/GPU": "nllb",
    "Google Translate · online": "google",
    "DeepL API · online": "deepl",
    "Gemini · online": "gemini",
}

WHISPER_DISPLAY = {
    "tiny · sehr schnell": "tiny",
    "base · leicht": "base",
    "small · guter Einstieg": "small",
    "medium · höhere Qualität": "medium",
    "large-v3 · beste Qualität": "large-v3",
    "turbo · schnell auf GPU": "turbo",
}

DEVICE_DISPLAY = {
    "Automatisch (empfohlen)": "auto",
    "Nur CPU": "cpu",
    "NVIDIA-GPU (CUDA)": "cuda",
}

TTS_ENGINE_DISPLAY = {
    "Edge TTS · online": "edge",
    "gTTS · Google online": "gtts",
    "Piper · lokal (ONNX-Modell)": "piper",
    "Chatterbox · eigene Stimme (lokal)": "chatterbox",
}

CHATTERBOX_MODEL_DISPLAY = {
    "V2 · stabile PyPI-Version": "v2",
}

TTS_PRIVACY_INFO = {
    "edge": (
        "Edge TTS arbeitet online; die übersetzten Textabschnitte werden für die "
        "Spracherzeugung an Microsoft übertragen."
    ),
    "gtts": (
        "gTTS arbeitet online über Google Translate Text-to-Speech. Die übersetzten "
        "Textabschnitte werden an Google übertragen; echte Stimmklone sind dort nicht möglich."
    ),
    "piper": (
        "Piper erzeugt die neue Stimme vollständig lokal aus dem gewählten ONNX-Stimmenmodell."
    ),
    "chatterbox": (
        "Chatterbox verarbeitet Text und Referenzaufnahme lokal. Die Ausgabe enthält "
        "ein nicht hörbares PerTh-Wasserzeichen."
    ),
}

COMPUTE_TYPE_DISPLAY = {
    "Automatisch": "default",
    "int8 · sparsam auf CPU": "int8",
    "float16 · schnell auf GPU": "float16",
    "float32 · maximale Genauigkeit": "float32",
}

STAGE_DISPLAY = {
    Stage.PREPARING: "Vorbereitung",
    Stage.EXTRACTING: "Audio extrahieren",
    Stage.TRANSCRIBING: "Sprache erkennen",
    Stage.OPTIMIZING: "Transkript optimieren",
    Stage.TRANSLATING: "Text übersetzen",
    Stage.SYNTHESIZING: "Stimme erzeugen",
    Stage.SYNCHRONIZING: "Tonspur synchronisieren",
    Stage.MUXING: "Video fertigstellen",
    Stage.COMPLETED: "Abgeschlossen",
}

AUTO_VOICE = "Automatisch passend zur Zielsprache"

PROVIDER_INFO = {
    "nllb": (
        "LOKAL",
        "success-inverse",
        "Lokale NLLB-Übersetzung",
        "Die Übersetzung läuft auf diesem Computer. Beim ersten Start wird das Modell geladen.",
    ),
    "google": (
        "ONLINE",
        "info-inverse",
        "Google Translate",
        "Schneller Einstieg ohne API-Schlüssel. Die erkannten Textabschnitte werden an "
        "den öffentlichen Google-Übersetzungsdienst gesendet.",
    ),
    "deepl": (
        "ONLINE",
        "warning-inverse",
        "DeepL API",
        "Verwendet ausschließlich DeepL und benötigt einen API-Schlüssel. Nicht jede "
        "Zielsprache aus der allgemeinen Liste wird von DeepL angeboten.",
    ),
    "gemini": (
        "ONLINE",
        "warning-inverse",
        "Gemini",
        "Übersetzt cue-stabil mit strukturierten Antworten. Dafür wird ein Gemini-API-"
        "Schlüssel benötigt.",
    ),
}


@dataclass(frozen=True, slots=True)
class _GuiError:
    summary: str
    details: str


class LinguoAIApp:
    def __init__(self, root: ttk.Window) -> None:
        self.root = root
        self.root.title("LinguoAI · Video Dubbing")
        self._fit_initial_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.source_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.target_language = tk.StringVar(value=self._language_display("de"))
        self.source_language = tk.StringVar(value="Automatisch erkennen")
        self.service = tk.StringVar(value=self._default_service_display())
        self.whisper_model = tk.StringVar(value="small · guter Einstieg")
        self.device = tk.StringVar(value="Automatisch (empfohlen)")
        self.compute_type = tk.StringVar(value="Automatisch")
        self.nllb_device = tk.StringVar(value="Automatisch (empfohlen)")
        self.nllb_model = tk.StringVar(value="facebook/nllb-200-distilled-600M")
        self.nllb_revision = tk.StringVar()
        self.nllb_source_code = tk.StringVar()
        self.nllb_target_code = tk.StringVar()
        self.nllb_batch_size = tk.IntVar(value=4)
        self.nllb_max_input_tokens = tk.IntVar(value=512)
        self.nllb_max_new_tokens = tk.IntVar(value=256)
        self.tts_engine = tk.StringVar(value="Edge TTS · online")
        self.tts_device = tk.StringVar(value="Automatisch (empfohlen)")
        self.tts_voice = tk.StringVar(value=AUTO_VOICE)
        self.tts_reference_audio = tk.StringVar()
        self.confirm_voice_rights = tk.BooleanVar(value=False)
        self.piper_model = tk.StringVar()
        self.piper_config = tk.StringVar()
        self.piper_speaker_id = tk.StringVar()
        self.piper_length_scale = tk.DoubleVar(value=1.0)
        self.chatterbox_model = tk.StringVar(value="V2 · stabile PyPI-Version")
        self.chatterbox_exaggeration = tk.DoubleVar(value=0.5)
        self.chatterbox_cfg_weight = tk.DoubleVar(value=0.5)
        self.transcription_workers = tk.IntVar(value=1)
        self.tts_workers = tk.IntVar(value=4)
        self.max_tts_speed = tk.DoubleVar(value=2.2)
        self.request_timeout = tk.DoubleVar(value=60.0)
        self.request_retries = tk.IntVar(value=4)
        self.optimize = tk.BooleanVar(value=False)
        self.overwrite = tk.BooleanVar(value=False)
        self.show_keys = tk.BooleanVar(value=False)
        self.gemini_key = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        self.gemini_model = tk.StringVar(value="gemini-3.5-flash")
        self.deepl_key = tk.StringVar(value=os.environ.get("DEEPL_API_KEY", ""))

        self.progress = tk.DoubleVar(value=0)
        self.progress_percent = tk.StringVar(value="0 %")
        self.status = tk.StringVar(value="Bereit für ein Video")
        self.stage = tk.StringVar(value="Noch nicht gestartet")
        self.file_progress = tk.StringVar(value="")
        self.provider_title = tk.StringVar()
        self.provider_description = tk.StringVar()
        self.job_summary = tk.StringVar()
        self.environment_status = tk.StringVar(value="System wird geprüft …")
        self.result_summary = tk.StringVar(value="Nach dem Lauf erscheinen hier die Ergebnisse.")

        self._events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self._pipeline: VideoDubbingPipeline | None = None
        self._pipeline_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel_requested = threading.Event()
        self._running = False
        self._closing = False
        self._dark_theme = True
        self._source_mode: str | None = None
        self._output_is_automatic = True
        self._setting_output_programmatically = False
        self._last_video_path: Path | None = None
        self._last_result_folder: Path | None = None
        self._interactive_widgets: list[tuple[tk.Widget, str]] = []
        self._provider_widgets: dict[str, list[tuple[tk.Widget, str]]] = {
            "deepl": [],
            "gemini": [],
            "nllb": [],
        }
        self._tts_widgets: dict[str, list[tuple[tk.Widget, str]]] = {
            "edge": [],
            "gtts": [],
            "piper": [],
            "chatterbox": [],
        }
        self._tts_panels: dict[str, list[tk.Widget]] = {
            "edge": [],
            "gtts": [],
            "piper": [],
            "chatterbox": [],
        }

        self._configure_styles()
        self._build_ui()
        self._bind_events()
        self._update_provider_controls()
        self._update_tts_controls()
        self._refresh_summary()
        self._style_log()
        self.root.after(100, self._poll_events)
        self.root.after(250, self._check_environment)

    def _fit_initial_window(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1180, max(640, screen_width - 90))
        height = min(850, max(480, screen_height - 110))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(940, screen_width - 40), min(640, screen_height - 70))

    def _configure_styles(self) -> None:
        style = self.root.style
        style.configure("Hero.TLabel", font=("Segoe UI Variable Display", 22, "bold"))
        muted = style.colors.light if self._dark_theme else style.colors.secondary
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground=muted)
        style.configure("Muted.TLabel", foreground=muted)
        style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Percent.TLabel", font=("Segoe UI Variable Display", 22, "bold"))
        style.configure("success.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 10))
        style.configure("TNotebook.Tab", padding=(14, 8))

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=(22, 15, 22, 14))
        shell.pack(fill=tk.BOTH, expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        self._build_header(shell)

        self.notebook = ttk.Notebook(shell, bootstyle="primary")
        self.notebook.grid(row=1, column=0, sticky="nsew", pady=(12, 12))

        workflow_tab = ttk.Frame(self.notebook)
        advanced_tab = ttk.Frame(self.notebook)
        log_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(workflow_tab, text="  Start  ")
        self.notebook.add(advanced_tab, text="  Erweitert  ")
        self.notebook.add(log_tab, text="  Protokoll  ")

        workflow_scroll = ScrolledFrame(
            workflow_tab, padding=14, autohide=False, bootstyle="default"
        )
        workflow_scroll.pack(fill=tk.BOTH, expand=True)
        advanced_scroll = ScrolledFrame(
            advanced_tab, padding=14, autohide=False, bootstyle="default"
        )
        advanced_scroll.pack(fill=tk.BOTH, expand=True)

        self._build_workflow_tab(workflow_scroll)
        self._build_advanced_tab(advanced_scroll)
        self._build_log_tab(log_tab)
        self._build_footer(shell)

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(
            header,
            text="L",
            font=("Segoe UI Variable Display", 19, "bold"),
            padding=(13, 7),
            bootstyle="primary-inverse",
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
        ttk.Label(header, text="LinguoAI", style="Hero.TLabel").grid(row=0, column=1, sticky="sw")
        ttk.Label(
            header,
            text="Video übersetzen, neu vertonen und Untertitel erzeugen",
            style="Subtitle.TLabel",
        ).grid(row=1, column=1, sticky="nw")

        self.run_badge = ttk.Label(
            header,
            text="BEREIT",
            padding=(11, 5),
            bootstyle="success-inverse",
        )
        self.run_badge.grid(row=0, column=2, rowspan=2, padx=(12, 8))
        self.theme_button = ttk.Button(
            header,
            text="Helles Design",
            command=self._toggle_theme,
            bootstyle="info-outline",
        )
        self.theme_button.grid(row=0, column=3, rowspan=2)

    def _build_workflow_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=3, uniform="workflow")
        tab.columnconfigure(1, weight=2, uniform="workflow")
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        self._build_path_card(left)
        self._build_language_card(left)
        self._build_voice_card(left)
        self._build_provider_card(right)
        self._build_environment_card(right)
        self._build_progress_card(right)

    def _build_path_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="01  Video und Ausgabe", padding=12)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Quelle", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        source_row = ttk.Frame(card)
        source_row.grid(row=1, column=0, sticky="ew", pady=(4, 9))
        source_row.columnconfigure(0, weight=1)
        self.source_entry = ttk.Entry(source_row, textvariable=self.source_path)
        self.source_entry.grid(row=0, column=0, sticky="ew")
        source_file = ttk.Button(
            source_row, text="Datei …", command=self._select_file, bootstyle="primary"
        )
        source_file.grid(row=0, column=1, padx=(7, 4))
        source_folder = ttk.Button(
            source_row,
            text="Ordner …",
            command=self._select_folder,
            bootstyle="primary-outline",
        )
        source_folder.grid(row=0, column=2)

        ttk.Label(card, text="Ausgabe", style="Muted.TLabel").grid(row=2, column=0, sticky="w")
        output_row = ttk.Frame(card)
        output_row.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        output_row.columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(output_row, textvariable=self.output_path)
        self.output_entry.grid(row=0, column=0, sticky="ew")
        output_button = ttk.Button(
            output_row,
            text="Auswählen …",
            command=self._select_output,
            bootstyle="primary-outline",
        )
        output_button.grid(row=0, column=1, padx=(7, 0))

        for widget, state in (
            (self.source_entry, "normal"),
            (source_file, "normal"),
            (source_folder, "normal"),
            (self.output_entry, "normal"),
            (output_button, "normal"),
        ):
            self._register(widget, state)

    def _build_language_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="02  Sprache und Übersetzung", padding=12)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        language_values = [self._language_display(code) for code in self._sorted_language_codes()]
        ttk.Label(card, text="Zielsprache", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        ttk.Label(card, text="Quellsprache", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        target_combo = ttk.Combobox(
            card,
            textvariable=self.target_language,
            values=language_values,
            state="normal",
        )
        target_combo.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(4, 9))
        source_combo = ttk.Combobox(
            card,
            textvariable=self.source_language,
            values=["Automatisch erkennen", *language_values],
            state="normal",
        )
        source_combo.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(4, 9))

        ttk.Label(card, text="Übersetzungsart", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w"
        )
        self.service_combo = ttk.Combobox(
            card,
            textvariable=self.service,
            values=list(SERVICE_DISPLAY),
            state="readonly",
        )
        self.service_combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self._register(target_combo, "normal")
        self._register(source_combo, "normal")
        self._register(self.service_combo, "readonly")

    def _build_voice_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="03  Erkennung und Stimme", padding=12)
        card.grid(row=2, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="Spracherkennungsmodell", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        ttk.Label(card, text="TTS-Engine", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        model_combo = ttk.Combobox(
            card,
            textvariable=self.whisper_model,
            values=list(WHISPER_DISPLAY),
            state="normal",
        )
        model_combo.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(4, 9))
        engine_combo = ttk.Combobox(
            card,
            textvariable=self.tts_engine,
            values=list(TTS_ENGINE_DISPLAY),
            state="readonly",
        )
        engine_combo.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(4, 9))

        engine_details = ttk.Frame(card)
        engine_details.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 9))
        engine_details.columnconfigure(0, weight=1)

        edge_panel = ttk.Frame(engine_details)
        edge_panel.grid(row=0, column=0, sticky="ew")
        edge_panel.columnconfigure(0, weight=1)
        ttk.Label(edge_panel, text="Edge-Stimme", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        voice_values = [AUTO_VOICE, *sorted(set(DEFAULT_EDGE_VOICES.values()))]
        voice_combo = ttk.Combobox(
            edge_panel,
            textvariable=self.tts_voice,
            values=voice_values,
            state="normal",
        )
        voice_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        gtts_panel = ttk.Frame(engine_details)
        gtts_panel.grid(row=0, column=0, sticky="ew")
        gtts_panel.columnconfigure(0, weight=1)
        ttk.Label(gtts_panel, text="gTTS-Sprache", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        gtts_voice = ttk.Entry(gtts_panel, textvariable=self.tts_voice)
        gtts_voice.grid(row=1, column=0, sticky="ew", pady=(4, 3))
        ttk.Label(
            gtts_panel,
            text="Leer lassen für Zielsprache automatisch. Optional z. B. de, en, fr oder es.",
            justify=tk.LEFT,
            style="Muted.TLabel",
        ).grid(row=2, column=0, sticky="ew")

        piper_panel = ttk.Frame(engine_details)
        piper_panel.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            piper_panel,
            text=(
                "Piper nutzt dein lokales ONNX-Stimmenmodell. Modell, Sprecher-ID und "
                "Tempo stellst du unter „Erweitert“ ein."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        ).pack(fill=tk.X)

        chatterbox_panel = ttk.Frame(engine_details)
        chatterbox_panel.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            chatterbox_panel,
            text=(
                "Chatterbox übernimmt die Stimme aus deiner Referenzaufnahme. "
                "Aufnahme und Einwilligung stellst du unter „Erweitert“ ein."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        ).pack(fill=tk.X)

        self._tts_panels["edge"].append(edge_panel)
        self._tts_panels["gtts"].append(gtts_panel)
        self._tts_panels["piper"].append(piper_panel)
        self._tts_panels["chatterbox"].append(chatterbox_panel)

        optimize = ttk.Checkbutton(
            card,
            text="Transkript optional mit Gemini korrigieren",
            variable=self.optimize,
            bootstyle="round-toggle",
        )
        optimize.grid(row=3, column=0, sticky="w", padx=(0, 6))
        overwrite = ttk.Checkbutton(
            card,
            text="Vorhandene Ausgaben ersetzen",
            variable=self.overwrite,
            bootstyle="round-toggle",
        )
        overwrite.grid(row=3, column=1, sticky="w", padx=(6, 0))

        for widget, state in (
            (model_combo, "normal"),
            (engine_combo, "readonly"),
            (optimize, "normal"),
            (overwrite, "normal"),
        ):
            self._register(widget, state)
        self._register(voice_combo, "normal", tts_engine="edge")
        self._register(gtts_voice, "normal", tts_engine="gtts")

    def _build_provider_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Gewählter Modus", padding=12)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        card.columnconfigure(0, weight=1)

        provider_header = ttk.Frame(card)
        provider_header.grid(row=0, column=0, sticky="ew")
        provider_header.columnconfigure(0, weight=1)
        ttk.Label(provider_header, textvariable=self.provider_title, style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.provider_badge = ttk.Label(provider_header, text="LOKAL", padding=(8, 3))
        self.provider_badge.grid(row=0, column=1, sticky="e")
        self.provider_description_label = ttk.Label(
            card,
            textvariable=self.provider_description,
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        self.provider_description_label.grid(row=1, column=0, sticky="ew", pady=(7, 8))
        self._bind_dynamic_wrap(self.provider_description_label, card, inset=24)
        ttk.Separator(card).grid(row=2, column=0, sticky="ew", pady=(0, 7))
        self.job_summary_label = ttk.Label(
            card,
            textvariable=self.job_summary,
            justify=tk.LEFT,
        )
        self.job_summary_label.grid(row=3, column=0, sticky="ew")
        self._bind_dynamic_wrap(self.job_summary_label, card, inset=24)

    def _build_environment_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Systemcheck", padding=12)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        card.columnconfigure(0, weight=1)
        self.environment_label = ttk.Label(
            card,
            textvariable=self.environment_status,
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        self.environment_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._bind_dynamic_wrap(self.environment_label, card, inset=110)
        check_button = ttk.Button(
            card,
            text="Neu prüfen",
            command=self._check_environment,
            bootstyle="primary-outline",
        )
        check_button.grid(row=0, column=1, sticky="e")

    def _build_progress_card(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Fortschritt und Ergebnis", padding=12)
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(5, weight=1)

        progress_header = ttk.Frame(card)
        progress_header.grid(row=0, column=0, sticky="ew")
        progress_header.columnconfigure(0, weight=1)
        ttk.Label(progress_header, textvariable=self.stage, style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(progress_header, textvariable=self.progress_percent, style="Percent.TLabel").grid(
            row=0, column=1, sticky="e"
        )
        self.progress_bar = ttk.Progressbar(
            card,
            variable=self.progress,
            maximum=100,
            mode="determinate",
            bootstyle="success-striped",
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(7, 7))
        ttk.Label(card, textvariable=self.status).grid(row=2, column=0, sticky="w")
        ttk.Label(card, textvariable=self.file_progress, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(2, 8)
        )
        ttk.Separator(card).grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self.result_label = ttk.Label(
            card,
            textvariable=self.result_summary,
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        self.result_label.grid(row=5, column=0, sticky="new")
        self._bind_dynamic_wrap(self.result_label, card, inset=24)

        result_actions = ttk.Frame(card)
        result_actions.grid(row=6, column=0, sticky="ew", pady=(9, 0))
        result_actions.columnconfigure(0, weight=1)
        result_actions.columnconfigure(1, weight=1)
        self.open_video_button = ttk.Button(
            result_actions,
            text="Video öffnen",
            command=self._open_last_video,
            state=tk.DISABLED,
            bootstyle="success-outline",
        )
        self.open_video_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.open_folder_button = ttk.Button(
            result_actions,
            text="Ordner öffnen",
            command=self._open_result_folder,
            state=tk.DISABLED,
            bootstyle="primary-outline",
        )
        self.open_folder_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _build_advanced_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1, uniform="advanced")
        tab.columnconfigure(1, weight=1, uniform="advanced")

        performance = ttk.LabelFrame(tab, text="Leistung", padding=12)
        performance.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 9))
        performance.columnconfigure(0, weight=1, uniform="performance")
        performance.columnconfigure(1, weight=1, uniform="performance")

        device_combo = self._form_combo(
            performance,
            "Hardware für Spracherkennung",
            self.device,
            list(DEVICE_DISPLAY),
            row=0,
            column=0,
        )
        compute_combo = self._form_combo(
            performance,
            "Rechengenauigkeit",
            self.compute_type,
            list(COMPUTE_TYPE_DISPLAY),
            row=0,
            column=1,
        )
        asr_workers = self._form_spinbox(
            performance,
            "Whisper-Worker",
            self.transcription_workers,
            1,
            8,
            row=2,
            column=0,
        )
        tts_workers = self._form_spinbox(
            performance,
            "Edge-TTS-Worker",
            self.tts_workers,
            1,
            16,
            row=2,
            column=1,
        )
        self._register(device_combo, "readonly")
        self._register(compute_combo, "readonly")
        self._register(asr_workers, "normal")
        self._register(tts_workers, "normal", tts_engine="edge")

        nllb = ttk.LabelFrame(tab, text="Lokales NLLB-Modell", padding=12)
        nllb.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 9))
        nllb.columnconfigure(0, weight=1, uniform="nllb")
        nllb.columnconfigure(1, weight=1, uniform="nllb")

        ttk.Label(nllb, text="Modell-ID oder lokaler Ordner", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        model_row = ttk.Frame(nllb)
        model_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        model_row.columnconfigure(0, weight=1)
        nllb_model_entry = ttk.Entry(model_row, textvariable=self.nllb_model)
        nllb_model_entry.grid(row=0, column=0, sticky="ew")
        nllb_browse = ttk.Button(
            model_row,
            text="Ordner …",
            command=self._select_nllb_model,
            bootstyle="primary-outline",
        )
        nllb_browse.grid(row=0, column=1, padx=(7, 0))
        nllb_revision = self._form_entry(
            nllb, "Revision (optional)", self.nllb_revision, row=2, column=0
        )
        nllb_device = self._form_combo(
            nllb,
            "Hardware für NLLB",
            self.nllb_device,
            list(DEVICE_DISPLAY),
            row=2,
            column=1,
        )
        nllb_source = self._form_entry(
            nllb, "FLORES-Quelle", self.nllb_source_code, row=4, column=0
        )
        nllb_target = self._form_entry(nllb, "FLORES-Ziel", self.nllb_target_code, row=4, column=1)
        nllb_batch = self._form_spinbox(
            nllb, "Batchgröße", self.nllb_batch_size, 1, 64, row=6, column=0
        )
        token_frame = ttk.Frame(nllb)
        token_frame.grid(row=6, column=1, rowspan=2, sticky="ew", padx=(7, 0))
        token_frame.columnconfigure(0, weight=1, uniform="tokens")
        token_frame.columnconfigure(1, weight=1, uniform="tokens")
        ttk.Label(token_frame, text="Eingabe", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(token_frame, text="Ausgabe", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(5, 0)
        )
        nllb_input_tokens = ttk.Spinbox(
            token_frame,
            from_=32,
            to=512,
            increment=16,
            textvariable=self.nllb_max_input_tokens,
            width=7,
        )
        nllb_input_tokens.grid(row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 5))
        nllb_output_tokens = ttk.Spinbox(
            token_frame,
            from_=16,
            to=512,
            increment=16,
            textvariable=self.nllb_max_new_tokens,
            width=7,
        )
        nllb_output_tokens.grid(row=1, column=1, sticky="ew", pady=(4, 0), padx=(5, 0))

        for widget, state in (
            (nllb_model_entry, "normal"),
            (nllb_browse, "normal"),
            (nllb_revision, "normal"),
            (nllb_device, "readonly"),
            (nllb_source, "normal"),
            (nllb_target, "normal"),
            (nllb_batch, "normal"),
            (nllb_input_tokens, "normal"),
            (nllb_output_tokens, "normal"),
        ):
            self._register(widget, state, provider="nllb")

        tts = ttk.LabelFrame(tab, text="Lokale Stimme und TTS-Engine", padding=12)
        tts.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 9))
        tts.columnconfigure(0, weight=1, uniform="tts")
        tts.columnconfigure(1, weight=1, uniform="tts")

        tts_device = self._form_combo(
            tts,
            "Hardware für die Sprachausgabe",
            self.tts_device,
            list(DEVICE_DISPLAY),
            row=0,
            column=0,
        )
        self.tts_device_combo = tts_device
        tts_device_note = ttk.Label(
            tts,
            text=(
                "Automatisch nutzt CUDA, wenn die lokale Engine und deine Installation "
                "es unterstützen; Edge TTS rechnet im Onlinedienst."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        tts_device_note.grid(row=0, column=1, rowspan=2, sticky="new", padx=(7, 0), pady=(1, 0))
        self._bind_dynamic_wrap(tts_device_note, tts, inset=480)
        self._register(tts_device, "readonly")

        tts_settings = ttk.Frame(tts)
        tts_settings.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        tts_settings.columnconfigure(0, weight=1)

        edge_settings = ttk.Frame(tts_settings)
        edge_settings.grid(row=0, column=0, sticky="ew")
        edge_note = ttk.Label(
            edge_settings,
            text=(
                "Edge TTS benötigt kein lokales Stimmenmodell. Die Stimme wählst du im "
                "Tab „Start“; die Textabschnitte werden online vertont."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        edge_note.pack(fill=tk.X)
        self._tts_panels["edge"].append(edge_settings)

        gtts_settings = ttk.Frame(tts_settings)
        gtts_settings.grid(row=0, column=0, sticky="ew")
        gtts_note = ttk.Label(
            gtts_settings,
            text=(
                "gTTS nutzt Google Translate Text-to-Speech online. Es gibt keine einzelnen "
                "Stimmen wie bei Edge; im Start-Tab kann optional ein Sprachcode eingetragen "
                "werden."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        gtts_note.pack(fill=tk.X)
        self._tts_panels["gtts"].append(gtts_settings)

        piper_settings = ttk.Frame(tts_settings)
        piper_settings.grid(row=0, column=0, sticky="ew")
        piper_settings.columnconfigure(0, weight=1, uniform="piper")
        piper_settings.columnconfigure(1, weight=1, uniform="piper")

        ttk.Label(piper_settings, text="Piper-Stimmenmodell (.onnx)", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        piper_model_row = ttk.Frame(piper_settings)
        piper_model_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        piper_model_row.columnconfigure(0, weight=1)
        piper_model_entry = ttk.Entry(piper_model_row, textvariable=self.piper_model)
        piper_model_entry.grid(row=0, column=0, sticky="ew")
        piper_model_browse = ttk.Button(
            piper_model_row,
            text="ONNX …",
            command=self._select_piper_model,
            bootstyle="primary-outline",
        )
        piper_model_browse.grid(row=0, column=1, padx=(7, 0))

        ttk.Label(
            piper_settings,
            text="Modellkonfiguration (.json, optional)",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w")
        piper_config_row = ttk.Frame(piper_settings)
        piper_config_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        piper_config_row.columnconfigure(0, weight=1)
        piper_config_entry = ttk.Entry(piper_config_row, textvariable=self.piper_config)
        piper_config_entry.grid(row=0, column=0, sticky="ew")
        piper_config_browse = ttk.Button(
            piper_config_row,
            text="JSON …",
            command=self._select_piper_config,
            bootstyle="primary-outline",
        )
        piper_config_browse.grid(row=0, column=1, padx=(7, 0))

        piper_speaker = self._form_entry(
            piper_settings,
            "Sprecher-ID (optional)",
            self.piper_speaker_id,
            row=4,
            column=0,
        )
        piper_length = self._form_spinbox(
            piper_settings,
            "Längenfaktor",
            self.piper_length_scale,
            0.25,
            4.0,
            row=4,
            column=1,
            increment=0.05,
        )
        piper_note = ttk.Label(
            piper_settings,
            text=(
                "Ohne eigenen JSON-Pfad wird <modell>.onnx.json verwendet. Ein kleinerer "
                "Längenfaktor spricht schneller."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        piper_note.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        self._bind_dynamic_wrap(piper_note, piper_settings, inset=24)
        self._tts_panels["piper"].append(piper_settings)
        for widget, state in (
            (piper_model_entry, "normal"),
            (piper_model_browse, "normal"),
            (piper_config_entry, "normal"),
            (piper_config_browse, "normal"),
            (piper_speaker, "normal"),
            (piper_length, "normal"),
        ):
            self._register(widget, state, tts_engine="piper")

        chatterbox_settings = ttk.Frame(tts_settings)
        chatterbox_settings.grid(row=0, column=0, sticky="ew")
        chatterbox_settings.columnconfigure(0, weight=1, uniform="chatterbox")
        chatterbox_settings.columnconfigure(1, weight=1, uniform="chatterbox")

        ttk.Label(
            chatterbox_settings,
            text="Referenzaufnahme der gewünschten Stimme",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        reference_row = ttk.Frame(chatterbox_settings)
        reference_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        reference_row.columnconfigure(0, weight=1)
        reference_entry = ttk.Entry(reference_row, textvariable=self.tts_reference_audio)
        reference_entry.grid(row=0, column=0, sticky="ew")
        reference_browse = ttk.Button(
            reference_row,
            text="Audio …",
            command=self._select_voice_reference,
            bootstyle="primary-outline",
        )
        reference_browse.grid(row=0, column=1, padx=(7, 0))

        chatterbox_model = self._form_combo(
            chatterbox_settings,
            "Modellgeneration",
            self.chatterbox_model,
            list(CHATTERBOX_MODEL_DISPLAY),
            row=2,
            column=0,
        )
        chatterbox_exaggeration = self._form_spinbox(
            chatterbox_settings,
            "Ausdruck",
            self.chatterbox_exaggeration,
            0.0,
            2.0,
            row=2,
            column=1,
            increment=0.05,
        )
        chatterbox_cfg = self._form_spinbox(
            chatterbox_settings,
            "CFG-Stärke",
            self.chatterbox_cfg_weight,
            0.0,
            1.0,
            row=4,
            column=0,
            increment=0.05,
        )
        voice_rights = ttk.Checkbutton(
            chatterbox_settings,
            text="Ich habe die Erlaubnis, diese Stimme zu verwenden.",
            variable=self.confirm_voice_rights,
            bootstyle="round-toggle",
        )
        voice_rights.grid(row=4, column=1, rowspan=2, sticky="w", padx=(7, 0), pady=(18, 8))
        chatterbox_note = ttk.Label(
            chatterbox_settings,
            text=(
                "Empfohlen: 6 bis 10 Sekunden saubere Sprache ohne Musik oder Hall. "
                "Chatterbox versieht erzeugtes Audio mit einem nicht hörbaren "
                "PerTh-Wasserzeichen. Nutze nur Stimmen mit ausdrücklicher Erlaubnis."
            ),
            justify=tk.LEFT,
            bootstyle="warning",
        )
        chatterbox_note.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        self._bind_dynamic_wrap(chatterbox_note, chatterbox_settings, inset=24)
        self._tts_panels["chatterbox"].append(chatterbox_settings)
        for widget, state in (
            (reference_entry, "normal"),
            (reference_browse, "normal"),
            (chatterbox_model, "readonly"),
            (chatterbox_exaggeration, "normal"),
            (chatterbox_cfg, "normal"),
            (voice_rights, "normal"),
        ):
            self._register(widget, state, tts_engine="chatterbox")

        credentials = ttk.LabelFrame(tab, text="Online-Dienste und Schlüssel", padding=12)
        credentials.grid(row=2, column=0, sticky="nsew", padx=(0, 7))
        credentials.columnconfigure(0, weight=1, uniform="credentials")
        credentials.columnconfigure(1, weight=1, uniform="credentials")

        self.deepl_entry = self._form_entry(
            credentials,
            "DeepL API-Key",
            self.deepl_key,
            row=0,
            column=0,
            show="•",
        )
        self.gemini_entry = self._form_entry(
            credentials,
            "Gemini API-Key",
            self.gemini_key,
            row=0,
            column=1,
            show="•",
        )
        gemini_model = self._form_entry(
            credentials, "Gemini-Modell", self.gemini_model, row=2, column=1
        )
        show_keys = ttk.Checkbutton(
            credentials,
            text="Schlüssel anzeigen",
            variable=self.show_keys,
            command=self._toggle_key_visibility,
            bootstyle="round-toggle",
        )
        show_keys.grid(row=2, column=0, rowspan=2, sticky="w", pady=(18, 0))
        links = ttk.Frame(credentials)
        links.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(
            links,
            text="DeepL-Key anlegen",
            command=lambda: webbrowser.open_new("https://www.deepl.com/pro-api"),
            bootstyle="link",
        ).pack(side=tk.LEFT)
        ttk.Button(
            links,
            text="Gemini-Key anlegen",
            command=lambda: webbrowser.open_new("https://aistudio.google.com/app/apikey"),
            bootstyle="link",
        ).pack(side=tk.LEFT, padx=(6, 0))

        self._register(self.deepl_entry, "normal", provider="deepl")
        self._register(self.gemini_entry, "normal", provider="gemini")
        self._register(gemini_model, "normal", provider="gemini")
        self._register(show_keys, "normal")

        limits = ttk.LabelFrame(tab, text="Audio und Netzwerk", padding=12)
        limits.grid(row=2, column=1, sticky="nsew", padx=(7, 0))
        limits.columnconfigure(0, weight=1, uniform="limits")
        limits.columnconfigure(1, weight=1, uniform="limits")
        max_speed = self._form_spinbox(
            limits,
            "Maximales Sprechtempo",
            self.max_tts_speed,
            1.0,
            4.0,
            row=0,
            column=0,
            increment=0.1,
        )
        request_timeout = self._form_spinbox(
            limits,
            "Netzwerk-Timeout (Sek.)",
            self.request_timeout,
            5,
            600,
            row=0,
            column=1,
        )
        request_retries = self._form_spinbox(
            limits,
            "Versuche je Anfrage",
            self.request_retries,
            1,
            8,
            row=2,
            column=0,
        )
        limits_note = ttk.Label(
            limits,
            text=(
                "Niedrige NLLB-Batches sparen Speicher. Bei CPU-Betrieb sind Whisper small, "
                "int8 und ein NLLB-Batch von 2 bis 4 ein guter Start."
            ),
            justify=tk.LEFT,
            style="Muted.TLabel",
        )
        limits_note.grid(row=2, column=1, rowspan=2, sticky="new", padx=(7, 0), pady=(18, 0))
        self._bind_dynamic_wrap(limits_note, limits, inset=250)
        self._register(max_speed, "normal")
        self._register(request_timeout, "normal")
        self._register(request_retries, "normal")

    def _build_log_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        header = ttk.Frame(tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Technisches Protokoll", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            header,
            text="Kopieren",
            command=self._copy_log,
            bootstyle="primary-outline",
        ).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(
            header,
            text="Leeren",
            command=self._clear_log,
            bootstyle="primary-outline",
        ).grid(row=0, column=2)
        self.log = scrolledtext.ScrolledText(
            tab,
            height=14,
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            padx=12,
            pady=10,
            font=("Cascadia Mono", 10),
        )
        self.log.grid(row=1, column=0, sticky="nsew")

    def _build_footer(self, parent: ttk.Frame) -> None:
        footer = ttk.Frame(parent)
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Strg+O: Datei  ·  Strg+Enter: Start  ·  Esc: Abbrechen",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(
            footer,
            text="Abbrechen",
            command=self._cancel,
            bootstyle="danger-outline",
            state=tk.DISABLED,
        )
        self.cancel_button.grid(row=0, column=1, padx=(12, 7))
        self.start_button = ttk.Button(
            footer,
            text="Verarbeitung starten",
            command=self._start,
            bootstyle="success",
        )
        self.start_button.grid(row=0, column=2)

    def _form_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.Variable,
        *,
        row: int,
        column: int,
        show: str | None = None,
    ) -> ttk.Entry:
        padx = (0, 7) if column == 0 else (7, 0)
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row, column=column, sticky="w", padx=padx
        )
        entry = ttk.Entry(parent, textvariable=variable, show=show or "", width=18)
        entry.grid(row=row + 1, column=column, sticky="ew", padx=padx, pady=(4, 8))
        return entry

    def _form_combo(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.Variable,
        values: list[str],
        *,
        row: int,
        column: int,
    ) -> ttk.Combobox:
        padx = (0, 7) if column == 0 else (7, 0)
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row, column=column, sticky="w", padx=padx
        )
        combo = ttk.Combobox(
            parent, textvariable=variable, values=values, state="readonly", width=21
        )
        combo.grid(row=row + 1, column=column, sticky="ew", padx=padx, pady=(4, 8))
        return combo

    def _form_spinbox(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.Variable,
        minimum: float,
        maximum: float,
        *,
        row: int,
        column: int,
        increment: float = 1,
    ) -> ttk.Spinbox:
        padx = (0, 7) if column == 0 else (7, 0)
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row, column=column, sticky="w", padx=padx
        )
        spinbox = ttk.Spinbox(
            parent,
            from_=minimum,
            to=maximum,
            increment=increment,
            textvariable=variable,
            width=10,
        )
        spinbox.grid(row=row + 1, column=column, sticky="ew", padx=padx, pady=(4, 8))
        return spinbox

    def _register(
        self,
        widget: tk.Widget,
        idle_state: str,
        *,
        provider: str | None = None,
        tts_engine: str | None = None,
    ) -> None:
        item = (widget, idle_state)
        self._interactive_widgets.append(item)
        if provider:
            self._provider_widgets[provider].append(item)
        if tts_engine:
            self._tts_widgets[tts_engine].append(item)

    @staticmethod
    def _bind_dynamic_wrap(label: ttk.Label, container: ttk.Frame, *, inset: int) -> None:
        def update_wrap(event: tk.Event[tk.Misc]) -> None:
            label.configure(wraplength=max(180, event.width - inset))

        container.bind("<Configure>", update_wrap, add="+")

    def _bind_events(self) -> None:
        self.root.bind("<Control-o>", lambda _event: self._select_file())
        self.root.bind("<Control-Shift-O>", lambda _event: self._select_folder())
        self.root.bind("<Control-Return>", lambda _event: self._start())
        self.root.bind("<Escape>", lambda _event: self._cancel())
        self.root.bind("<F5>", lambda _event: self._check_environment())
        self.source_path.trace_add("write", self._source_value_changed)
        self.output_path.trace_add("write", self._output_value_changed)
        self.target_language.trace_add("write", self._selection_changed)
        self.source_language.trace_add("write", self._selection_changed)
        self.service.trace_add("write", self._provider_changed)
        self.whisper_model.trace_add("write", self._selection_changed)
        self.device.trace_add("write", self._hardware_changed)
        self.compute_type.trace_add("write", self._hardware_changed)
        self.nllb_device.trace_add("write", self._hardware_changed)
        self.tts_engine.trace_add("write", self._tts_engine_changed)
        self.tts_device.trace_add("write", self._hardware_changed)
        self.tts_voice.trace_add("write", self._selection_changed)
        self.tts_reference_audio.trace_add("write", self._tts_setting_changed)
        self.confirm_voice_rights.trace_add("write", self._tts_setting_changed)
        self.piper_model.trace_add("write", self._tts_setting_changed)
        self.piper_config.trace_add("write", self._tts_setting_changed)
        self.piper_speaker_id.trace_add("write", self._tts_setting_changed)
        self.piper_length_scale.trace_add("write", self._tts_setting_changed)
        self.chatterbox_model.trace_add("write", self._tts_setting_changed)
        self.chatterbox_exaggeration.trace_add("write", self._tts_setting_changed)
        self.chatterbox_cfg_weight.trace_add("write", self._tts_setting_changed)
        self.optimize.trace_add("write", self._provider_changed)

    def _selection_changed(self, *_args: object) -> None:
        self._update_provider_controls()
        self._update_tts_controls()
        self._refresh_summary()
        self._check_environment()
        if self._output_is_automatic and self.source_path.get().strip():
            self._set_suggested_output()

    def _provider_changed(self, *_args: object) -> None:
        self._update_provider_controls()
        self._refresh_summary()
        self._check_environment()

    def _hardware_changed(self, *_args: object) -> None:
        self._refresh_summary()
        self._check_environment()

    def _tts_engine_changed(self, *_args: object) -> None:
        self._update_tts_controls()
        self._update_provider_controls()
        self._refresh_summary()
        self._check_environment()

    def _tts_setting_changed(self, *_args: object) -> None:
        self._refresh_summary()
        self._check_environment()

    def _source_value_changed(self, *_args: object) -> None:
        value = self.source_path.get().strip()
        path = Path(value).expanduser() if value else None
        self._source_mode = "folder" if path and path.is_dir() else "file" if path else None
        self._refresh_summary()
        if not self._running and self._output_is_automatic and value:
            self._set_suggested_output()

    def _output_value_changed(self, *_args: object) -> None:
        if not self._setting_output_programmatically:
            self._output_is_automatic = False

    def _select_file(self) -> None:
        if self._running:
            return
        patterns = " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))
        selected = filedialog.askopenfilename(
            title="Video auswählen",
            filetypes=[("Videos", patterns), ("Alle Dateien", "*.*")],
        )
        if selected:
            self.source_path.set(selected)
            self._source_mode = "file"
            self._output_is_automatic = True
            self._set_suggested_output()

    def _select_folder(self) -> None:
        if self._running:
            return
        selected = filedialog.askdirectory(title="Videoordner auswählen")
        if selected:
            self.source_path.set(selected)
            self._source_mode = "folder"
            self._output_is_automatic = True
            self._set_suggested_output()

    def _set_suggested_output(self) -> None:
        source_value = self.source_path.get().strip()
        if not source_value:
            return
        try:
            target_code = self._target_code()
        except ValueError:
            return
        suggestion = self._suggest_output_path(
            Path(source_value).expanduser(), target_code, self._source_mode
        )
        self._setting_output_programmatically = True
        try:
            self.output_path.set(suggestion)
        finally:
            self._setting_output_programmatically = False

    @staticmethod
    def _suggest_output_path(source: Path, target_code: str, source_mode: str | None) -> str:
        is_folder = source_mode == "folder" or (source_mode is None and source.is_dir())
        if is_folder:
            if source.name:
                return str(source.with_name(f"{source.name}-translated-{target_code}"))
            return str(source / f"translated-{target_code}")
        return str(source.with_name(f"{source.stem}.translated-{target_code}.mp4"))

    def _select_output(self) -> None:
        if self._running:
            return
        source_value = self.source_path.get().strip()
        source = Path(source_value).expanduser() if source_value else None
        is_folder = self._source_mode == "folder" or bool(source and source.is_dir())
        if is_folder:
            selected = filedialog.askdirectory(title="Zielordner auswählen")
        else:
            selected = filedialog.asksaveasfilename(
                title="Zielvideo auswählen",
                defaultextension=".mp4",
                filetypes=[("MP4", "*.mp4"), ("Matroska", "*.mkv"), ("QuickTime", "*.mov")],
            )
        if selected:
            self.output_path.set(selected)
            self._output_is_automatic = False

    def _select_nllb_model(self) -> None:
        if self._running:
            return
        selected = filedialog.askdirectory(title="Lokales NLLB-Modell auswählen")
        if selected:
            self.nllb_model.set(selected)

    def _select_piper_model(self) -> None:
        if self._running:
            return
        selected = filedialog.askopenfilename(
            title="Piper-Stimmenmodell auswählen",
            filetypes=[("Piper ONNX-Modell", "*.onnx"), ("Alle Dateien", "*.*")],
        )
        if selected:
            self.piper_model.set(selected)
            inferred_config = Path(f"{selected}.json")
            if not self.piper_config.get().strip() and inferred_config.is_file():
                self.piper_config.set(str(inferred_config))

    def _select_piper_config(self) -> None:
        if self._running:
            return
        selected = filedialog.askopenfilename(
            title="Piper-Modellkonfiguration auswählen",
            filetypes=[("JSON-Konfiguration", "*.json"), ("Alle Dateien", "*.*")],
        )
        if selected:
            self.piper_config.set(selected)

    def _select_voice_reference(self) -> None:
        if self._running:
            return
        selected = filedialog.askopenfilename(
            title="Stimmreferenz auswählen",
            filetypes=[
                ("Audiodateien", "*.wav *.flac *.mp3 *.m4a *.ogg *.opus"),
                ("WAV", "*.wav"),
                ("Alle Dateien", "*.*"),
            ],
        )
        if selected:
            self.tts_reference_audio.set(selected)

    def _start(self) -> None:
        if self._running:
            return
        try:
            source, output, config = self._validated_job()
        except (LinguoAIError, KeyError, tk.TclError, ValueError) as exc:
            messagebox.showerror("Einstellungen prüfen", str(exc))
            return

        if (
            self.overwrite.get()
            and Path(output).expanduser().exists()
            and not messagebox.askyesno(
                "Ersetzen bestätigen",
                "Das Ziel existiert bereits. Vorhandene Ergebnisdateien wirklich ersetzen?",
            )
        ):
            return

        self._running = True
        self._closing = False
        self._cancel_requested.clear()
        self._last_video_path = None
        self._last_result_folder = None
        self._update_result_buttons()
        self.progress.set(0)
        self.progress_percent.set("0 %")
        self.stage.set("Vorbereitung")
        self.status.set("Pipeline und Modelle werden vorbereitet …")
        self.file_progress.set("Der erste Modellstart kann einige Minuten dauern.")
        self.result_summary.set("Verarbeitung läuft. Details stehen im Protokoll.")
        self.run_badge.configure(text="LÄUFT", bootstyle="primary-inverse")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        self._set_running_state(True)
        self._append_log("Job gestartet", "info")

        def worker() -> None:
            terminal_event: tuple[str, object]
            pipeline: VideoDubbingPipeline | None = None
            secrets = (
                config.gemini_api_key,
                config.deepl_api_key,
                os.environ.get("HF_TOKEN", ""),
                os.environ.get("HUGGING_FACE_HUB_TOKEN", ""),
            )
            try:
                pipeline = VideoDubbingPipeline(
                    config,
                    event_sink=lambda update: self._events.put(("progress", update)),
                    cancel_event=self._cancel_requested,
                )
                with self._pipeline_lock:
                    self._pipeline = pipeline
                result = pipeline.run_path(source, output)
                terminal_event = ("done", result)
            except CancelledError as exc:
                terminal_event = ("cancelled", str(exc))
            except LinguoAIError as exc:
                safe = redact_secrets(exc, *secrets)
                terminal_event = ("error", _GuiError(safe, safe))
            except Exception as exc:
                safe_summary = redact_secrets(exc, *secrets)
                safe_trace = redact_secrets(traceback.format_exc(), *secrets)
                terminal_event = (
                    "error",
                    _GuiError(
                        f"Unerwarteter Fehler: {safe_summary}",
                        f"{safe_summary}\n\n{safe_trace}",
                    ),
                )
            finally:
                with self._pipeline_lock:
                    if self._pipeline is pipeline:
                        self._pipeline = None
            self._events.put(terminal_event)

        self._worker = threading.Thread(target=worker, name="linguoai-job", daemon=False)
        self._worker.start()

    def _validated_job(self) -> tuple[str, str, AppConfig]:
        source = self.source_path.get().strip()
        output = self.output_path.get().strip()
        if not source or not output:
            raise ConfigurationError("Bitte zuerst eine Quelle und ein Ausgabeziel auswählen.")
        source_path = Path(source).expanduser()
        if not source_path.exists():
            raise ConfigurationError(f"Die Quelle existiert nicht: {source_path}")

        target_code = self._target_code()
        source_value = self.source_language.get().strip()
        source_code = (
            None
            if source_value in {"", "Automatisch", "Automatisch erkennen"}
            else self._code_from_display(source_value)
        )
        service_code = SERVICE_DISPLAY[self.service.get()]
        using_nllb = service_code == "nllb"
        device_code = DEVICE_DISPLAY[self.device.get()]
        compute_type = COMPUTE_TYPE_DISPLAY[self.compute_type.get()]
        tts_engine = self._tts_engine_code()
        tts_device = DEVICE_DISPLAY[self.tts_device.get()]
        if compute_type == "float16" and device_code != "cuda":
            raise ConfigurationError(
                "float16 benötigt die explizite Auswahl 'NVIDIA-GPU (CUDA)'. "
                "Für CPU oder Automatisch bitte 'Automatisch' oder 'int8' verwenden."
            )
        if service_code == "deepl" and target_code not in DEEPL_TARGET_LANGUAGE_MAP:
            raise ConfigurationError(
                f"DeepL unterstützt die Zielsprache {target_code!r} derzeit nicht. "
                "Bitte einen anderen Dienst wählen."
            )
        voice = self.tts_voice.get().strip() if tts_engine in {"edge", "gtts"} else ""
        voice = "" if voice == AUTO_VOICE else voice
        if tts_engine == "edge" and not voice and target_code not in DEFAULT_EDGE_VOICES:
            raise ConfigurationError(
                f"Für {target_code!r} ist keine Standardstimme hinterlegt. "
                "Bitte unter 'Stimme' eine Edge-TTS-Stimmen-ID eintragen."
            )
        if tts_engine == "chatterbox" and not self._chatterbox_supports_language(target_code):
            raise ConfigurationError(
                f"Chatterbox Multilingual unterstützt die Zielsprache {target_code!r} nicht. "
                "Bitte Edge TTS, ein passendes Piper-Modell oder eine andere Zielsprache wählen."
            )

        piper_speaker_text = self.piper_speaker_id.get().strip()
        try:
            piper_speaker_id = int(piper_speaker_text) if piper_speaker_text else None
        except ValueError as exc:
            raise ConfigurationError("Die Piper-Sprecher-ID muss eine ganze Zahl sein.") from exc

        piper_model_text = self.piper_model.get().strip()
        piper_config_text = self.piper_config.get().strip()
        reference_text = self.tts_reference_audio.get().strip()
        if using_nllb:
            missing = self._missing_modules(("torch", "transformers", "sentencepiece"))
            if missing:
                raise DependencyError(
                    "Lokale NLLB-Abhängigkeiten fehlen: "
                    + ", ".join(missing)
                    + '. Installiere sie mit: python -m pip install -e ".[desktop]"'
                )

        config = AppConfig(
            target_language=target_code,
            source_language=source_code,
            translation_service=service_code,
            whisper_model=WHISPER_DISPLAY.get(
                self.whisper_model.get(), self.whisper_model.get().strip()
            ),
            device=device_code,
            compute_type=compute_type,
            transcription_workers=self.transcription_workers.get(),
            tts_workers=self.tts_workers.get(),
            tts_engine=tts_engine,
            tts_device=tts_device,
            tts_voice=voice or None,
            tts_reference_audio=Path(reference_text) if reference_text else None,
            confirm_voice_rights=self.confirm_voice_rights.get(),
            piper_model=Path(piper_model_text) if piper_model_text else None,
            piper_config=Path(piper_config_text) if piper_config_text else None,
            piper_speaker_id=piper_speaker_id,
            piper_length_scale=self.piper_length_scale.get(),
            chatterbox_model=CHATTERBOX_MODEL_DISPLAY.get(
                self.chatterbox_model.get(), self.chatterbox_model.get().strip().lower()
            ),
            chatterbox_exaggeration=self.chatterbox_exaggeration.get(),
            chatterbox_cfg_weight=self.chatterbox_cfg_weight.get(),
            max_tts_speed=self.max_tts_speed.get(),
            optimize_transcript=self.optimize.get(),
            gemini_model=self.gemini_model.get(),
            gemini_api_key=self.gemini_key.get().strip(),
            deepl_api_key=self.deepl_key.get().strip(),
            nllb_model=self.nllb_model.get(),
            nllb_device=DEVICE_DISPLAY[self.nllb_device.get()] if using_nllb else "auto",
            nllb_batch_size=self.nllb_batch_size.get() if using_nllb else 8,
            nllb_max_input_tokens=self.nllb_max_input_tokens.get() if using_nllb else 512,
            nllb_max_new_tokens=self.nllb_max_new_tokens.get() if using_nllb else 256,
            nllb_source_code=(self.nllb_source_code.get() or None) if using_nllb else None,
            nllb_target_code=(self.nllb_target_code.get() or None) if using_nllb else None,
            nllb_revision=(self.nllb_revision.get() or None) if using_nllb else None,
            request_timeout_seconds=self.request_timeout.get(),
            request_retries=self.request_retries.get(),
            overwrite=self.overwrite.get(),
        ).validated()
        return source, output, config

    def _cancel(self) -> None:
        if not self._running or self._cancel_requested.is_set():
            return
        self._cancel_requested.set()
        with self._pipeline_lock:
            pipeline = self._pipeline
        if pipeline is not None:
            pipeline.cancel()
        self.stage.set("Abbruch")
        self.status.set("Abbruch wird sicher ausgeführt …")
        self.file_progress.set("Ein laufender Modell- oder Netzwerkaufruf darf noch zurückkehren.")
        self.run_badge.configure(text="ABBRUCH", bootstyle="warning-inverse")
        self.cancel_button.configure(state=tk.DISABLED)
        self._append_log("Abbruch angefordert", "warning")

    def _poll_events(self) -> None:
        handled = 0
        while handled < 150:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if event == "progress" and isinstance(payload, ProgressUpdate):
                self._handle_progress(payload)
            elif event == "done" and isinstance(payload, BatchResult):
                self._handle_done(payload)
            elif event == "cancelled":
                self._append_log(f"ABGEBROCHEN: {payload}", "warning")
                self._finish("Abgebrochen", badge="ABGEBROCHEN", style="warning-inverse")
                self.result_summary.set(
                    "Der Job wurde abgebrochen. Die Quelldatei blieb unverändert."
                )
                if not self._closing:
                    messagebox.showinfo("Abgebrochen", str(payload))
            elif event == "error" and isinstance(payload, _GuiError):
                self._append_log(f"FEHLER: {payload.details}", "error")
                self._finish("Verarbeitung fehlgeschlagen", badge="FEHLER", style="danger-inverse")
                self.result_summary.set(
                    "Der Job konnte nicht abgeschlossen werden. "
                    "Technische Details stehen im Protokoll."
                )
                if not self._closing:
                    messagebox.showerror("Verarbeitung fehlgeschlagen", payload.summary[:900])

        if self._closing and not self._running:
            self.root.destroy()
            return
        self.root.after(100, self._poll_events)

    def _handle_progress(self, update: ProgressUpdate) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress.set(update.percent)
        self.progress_percent.set(f"{update.percent:.0f} %")
        if not self._cancel_requested.is_set():
            self.stage.set(STAGE_DISPLAY.get(update.stage, update.stage.value.title()))
            self.status.set(update.message)
            self.file_progress.set(
                f"Datei {update.file_index} von {update.file_count}: {update.input_path.name}"
                if update.input_path and update.file_count > 1
                else update.input_path.name
                if update.input_path
                else ""
            )
        self._append_log(f"[{update.percent:6.2f}%] {update.message}")

    def _handle_done(self, result: BatchResult) -> None:
        completed = len(result.completed)
        failed = len(result.failed)
        if not completed and not failed:
            self._append_log("FEHLER: Pipeline lieferte kein Ergebnis.", "error")
            self._finish(
                "Kein Ergebnis erzeugt",
                badge="FEHLER",
                style="danger-inverse",
            )
            self.result_summary.set("Die Pipeline wurde beendet, hat aber kein Video erzeugt.")
            if not self._closing:
                messagebox.showerror(
                    "Kein Ergebnis",
                    "Die Verarbeitung wurde beendet, hat aber kein Video erzeugt.",
                )
            return
        warning_count = sum(len(item.warnings) for item in result.completed)
        for item in result.completed:
            self._append_log(f"ERSTELLT: {item.output_path}", "success")
            self._append_log(f"UNTERTITEL: {item.original_subtitles}")
            self._append_log(f"UNTERTITEL: {item.translated_subtitles}")
            for warning in item.warnings:
                self._append_log(f"WARNUNG: {warning}", "warning")
        for failure in result.failed:
            self._append_log(f"FEHLER {failure.input_path.name}: {failure.error}", "error")

        if result.completed:
            self._last_video_path = result.completed[0].output_path
            self._last_result_folder = result.completed[0].output_path.parent
        self._update_result_buttons()

        if failed:
            state = "Mit Fehlern abgeschlossen" if completed else "Verarbeitung fehlgeschlagen"
            badge = "TEILERGEBNIS" if completed else "FEHLER"
            style = "warning-inverse" if completed else "danger-inverse"
            self._finish(state, badge=badge, style=style, percent=100 if completed else None)
            self.result_summary.set(
                f"{completed} erfolgreich, {failed} fehlgeschlagen"
                + (f", {warning_count} Warnung(en)." if warning_count else ".")
            )
            if not self._closing:
                messagebox.showwarning(
                    "Mit Fehlern abgeschlossen",
                    f"{completed} Video(s) erfolgreich, {failed} fehlgeschlagen. "
                    "Details stehen im Protokoll.",
                )
            return

        self._finish(
            "Erfolgreich abgeschlossen",
            badge="FERTIG",
            style="success-inverse",
            percent=100,
        )
        self.result_summary.set(
            f"{completed} Video(s) erfolgreich erstellt"
            + (f" · {warning_count} Warnung(en) im Protokoll" if warning_count else ".")
        )
        if not self._closing:
            messagebox.showinfo(
                "Abgeschlossen",
                f"{completed} Video(s) wurden erfolgreich verarbeitet.",
            )

    def _finish(
        self,
        status: str,
        *,
        badge: str,
        style: str,
        percent: float | None = None,
    ) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        if percent is not None:
            self.progress.set(percent)
            self.progress_percent.set(f"{percent:.0f} %")
        self._running = False
        self._worker = None
        self.status.set(status)
        self.stage.set(status)
        self.file_progress.set("")
        self.run_badge.configure(text=badge, bootstyle=style)
        self._set_running_state(False)

    def _set_running_state(self, running: bool) -> None:
        for widget, idle_state in self._interactive_widgets:
            with suppress(tk.TclError):
                widget.configure(state=tk.DISABLED if running else idle_state)
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        if not running:
            self._update_provider_controls()
        self._update_tts_controls()
        self._update_result_buttons()

    def _update_provider_controls(self) -> None:
        if not hasattr(self, "provider_badge"):
            return
        service_code = SERVICE_DISPLAY.get(self.service.get(), "google")
        for provider, widgets in self._provider_widgets.items():
            enabled = provider == service_code
            if provider == "gemini" and self.optimize.get():
                enabled = True
            for widget, idle_state in widgets:
                with suppress(tk.TclError):
                    widget.configure(
                        state=tk.DISABLED if self._running or not enabled else idle_state
                    )

        badge, style, title, description = PROVIDER_INFO[service_code]
        try:
            target_code = self._target_code()
        except ValueError:
            target_code = ""
        if service_code == "deepl" and target_code not in DEEPL_TARGET_LANGUAGE_MAP:
            badge = "PRÜFEN"
            style = "danger-inverse"
            description = (
                f"DeepL unterstützt den Zielcode {target_code or '?'} nicht. "
                "Bitte die Zielsprache oder den Dienst wechseln."
            )
        tts_engine = self._tts_engine_code()
        description = f"{description}\n\nSprachausgabe: {TTS_PRIVACY_INFO[tts_engine]}"
        if (
            tts_engine == "chatterbox"
            and target_code
            and not self._chatterbox_supports_language(target_code)
        ):
            badge = "PRÜFEN"
            style = "danger-inverse"
            description += (
                f" Die Zielsprache {target_code!r} wird von Chatterbox Multilingual "
                "nicht unterstützt."
            )
        self.provider_badge.configure(text=badge, bootstyle=style)
        self.provider_title.set(title)
        self.provider_description.set(description)

    def _update_tts_controls(self) -> None:
        if not self._tts_panels:
            return
        engine = self._tts_engine_code()
        for panel_engine, panels in self._tts_panels.items():
            for panel in panels:
                with suppress(tk.TclError):
                    if panel_engine == engine:
                        panel.grid()
                    else:
                        panel.grid_remove()
        for widget_engine, widgets in self._tts_widgets.items():
            enabled = widget_engine == engine
            for widget, idle_state in widgets:
                with suppress(tk.TclError):
                    widget.configure(
                        state=tk.DISABLED if self._running or not enabled else idle_state
                    )
        if hasattr(self, "tts_device_combo"):
            with suppress(tk.TclError):
                self.tts_device_combo.configure(
                    state=(
                        tk.DISABLED if self._running or engine in {"edge", "gtts"} else "readonly"
                    )
                )

    def _refresh_summary(self) -> None:
        if not hasattr(self, "job_summary_label"):
            return
        source_name = Path(self.source_path.get().strip()).name or "Noch keine Quelle"
        try:
            target_code = self._target_code()
            target_name = LANGUAGES.get(target_code, target_code)
        except ValueError:
            target_name = "Zielsprache prüfen"
        source_value = self.source_language.get().strip()
        source_name_display = (
            "automatisch erkannt"
            if source_value in {"", "Automatisch", "Automatisch erkennen"}
            else source_value
        )
        model = WHISPER_DISPLAY.get(self.whisper_model.get(), self.whisper_model.get())
        tts_engine = self._tts_engine_code()
        if tts_engine == "edge":
            voice = self.tts_voice.get().strip()
            tts_detail = "automatische Stimme" if voice == AUTO_VOICE or not voice else voice
            tts_summary = f"Edge TTS · {tts_detail} · online"
        elif tts_engine == "gtts":
            voice = self.tts_voice.get().strip()
            tts_detail = "Zielsprache automatisch" if voice == AUTO_VOICE or not voice else voice
            tts_summary = f"gTTS · {tts_detail} · Google online"
        elif tts_engine == "piper":
            model_name = Path(self.piper_model.get().strip()).name or "Modell fehlt"
            tts_summary = f"Piper · {model_name} · {self.tts_device.get()}"
        else:
            reference_name = Path(self.tts_reference_audio.get().strip()).name or "Referenz fehlt"
            generation = CHATTERBOX_MODEL_DISPLAY.get(
                self.chatterbox_model.get(), self.chatterbox_model.get()
            ).upper()
            tts_summary = f"Chatterbox {generation} · {reference_name} · {self.tts_device.get()}"
        self.job_summary.set(
            f"{source_name}\n{source_name_display}  →  {target_name}\n"
            f"Whisper {model} · {self.device.get()}\nStimme: {tts_summary}"
        )

    def _check_environment(self) -> None:
        if not hasattr(self, "environment_label"):
            return
        missing: list[str] = []
        if not shutil.which("ffmpeg"):
            missing.append("FFmpeg")
        if not shutil.which("ffprobe"):
            missing.append("ffprobe")
        missing.extend(self._missing_modules(("faster_whisper",)))
        service_code = SERVICE_DISPLAY.get(self.service.get(), "google")
        if service_code == "nllb":
            missing.extend(self._missing_modules(("torch", "transformers", "sentencepiece")))
        elif service_code == "google":
            missing.extend(self._missing_modules(("deep_translator",)))
        elif service_code == "deepl":
            missing.extend(self._missing_modules(("deepl",)))
        if service_code == "gemini" or self.optimize.get():
            missing.extend(self._missing_modules(("google.genai",)))

        problems: list[str] = []
        tts_engine = self._tts_engine_code()
        if tts_engine == "edge":
            missing.extend(self._missing_modules(("edge_tts",)))
        elif tts_engine == "gtts":
            missing.extend(self._missing_modules(("gtts",)))
        elif tts_engine == "piper":
            missing.extend(self._missing_modules(("piper",)))
            model_text = self.piper_model.get().strip()
            if not model_text:
                problems.append("Piper-ONNX-Modell auswählen")
            else:
                model_path = Path(model_text).expanduser()
                if not model_path.is_file():
                    problems.append("Piper-ONNX-Modell nicht gefunden")
                elif model_path.suffix.casefold() != ".onnx":
                    problems.append("Piper-Modell muss eine .onnx-Datei sein")
                config_text = self.piper_config.get().strip()
                config_path = (
                    Path(config_text).expanduser() if config_text else Path(f"{model_path}.json")
                )
                if not config_path.is_file():
                    problems.append("Piper-JSON-Konfiguration nicht gefunden")
        else:
            missing.extend(self._missing_modules(("chatterbox", "torch", "torchaudio")))
            try:
                target_code = self._target_code()
            except ValueError:
                target_code = ""
            if target_code and not self._chatterbox_supports_language(target_code):
                problems.append(f"Chatterbox unterstützt {target_code!r} nicht")
            reference_text = self.tts_reference_audio.get().strip()
            if not reference_text:
                problems.append("Stimmreferenz auswählen")
            elif not Path(reference_text).expanduser().is_file():
                problems.append("Stimmreferenz nicht gefunden")
            if not self.confirm_voice_rights.get():
                problems.append("Erlaubnis zur Stimmnutzung bestätigen")

        missing = list(dict.fromkeys(missing))
        if missing or problems:
            status_parts = []
            if missing:
                status_parts.append("Fehlt: " + ", ".join(missing))
            if problems:
                status_parts.append("Prüfen: " + "; ".join(problems))
            self.environment_status.set("\n".join(status_parts))
            self.environment_label.configure(bootstyle="danger")
        elif (
            COMPUTE_TYPE_DISPLAY.get(self.compute_type.get()) == "float16"
            and DEVICE_DISPLAY.get(self.device.get()) != "cuda"
        ):
            self.environment_status.set("float16 benötigt die explizite Auswahl NVIDIA-GPU (CUDA).")
            self.environment_label.configure(bootstyle="danger")
        elif (
            DEVICE_DISPLAY.get(self.device.get()) == "cuda"
            or (service_code == "nllb" and DEVICE_DISPLAY.get(self.nllb_device.get()) == "cuda")
            or (
                tts_engine in {"piper", "chatterbox"}
                and DEVICE_DISPLAY.get(self.tts_device.get()) == "cuda"
            )
        ):
            self.environment_status.set(
                "Pakete vorhanden. CUDA-Treiber und Laufzeit werden beim Start geprüft."
            )
            self.environment_label.configure(bootstyle="warning")
        else:
            self.environment_status.set(
                "FFmpeg, Whisper, Übersetzung und die gewählte TTS-Engine sind einsatzbereit."
            )
            self.environment_label.configure(bootstyle="success")

    @staticmethod
    def _missing_modules(module_names: tuple[str, ...]) -> list[str]:
        missing: list[str] = []
        for module_name in module_names:
            try:
                available = importlib.util.find_spec(module_name) is not None
            except (ImportError, AttributeError, ValueError):
                available = False
            if not available:
                missing.append(module_name)
        return missing

    def _toggle_key_visibility(self) -> None:
        marker = "" if self.show_keys.get() else "•"
        self.deepl_entry.configure(show=marker)
        self.gemini_entry.configure(show=marker)

    def _toggle_theme(self) -> None:
        self._dark_theme = not self._dark_theme
        self.root.style.theme_use("darkly" if self._dark_theme else "flatly")
        self.theme_button.configure(text="Helles Design" if self._dark_theme else "Dunkles Design")
        self._configure_styles()
        self._style_log()

    def _style_log(self) -> None:
        if not hasattr(self, "log"):
            return
        colors = self.root.style.colors
        self.log.configure(
            background=colors.inputbg,
            foreground=colors.inputfg,
            insertbackground=colors.inputfg,
            selectbackground=colors.selectbg,
            selectforeground=colors.selectfg,
        )
        self.log.tag_configure("info", foreground=colors.info)
        self.log.tag_configure("success", foreground=colors.success)
        self.log.tag_configure("warning", foreground=colors.warning)
        self.log.tag_configure("error", foreground=colors.danger)

    def _append_log(self, message: str, tag: str | None = None) -> None:
        self.log.configure(state=tk.NORMAL)
        tags = (tag,) if tag else ()
        self.log.insert(tk.END, message.rstrip() + "\n", tags)
        line_count = int(self.log.index("end-1c").split(".", maxsplit=1)[0])
        if line_count > 2_000:
            self.log.delete("1.0", "201.0")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _copy_log(self) -> None:
        content = self.log.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.status.set("Protokoll in die Zwischenablage kopiert")

    def _clear_log(self) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    def _update_result_buttons(self) -> None:
        if not hasattr(self, "open_video_button"):
            return
        video_ready = bool(self._last_video_path and self._last_video_path.exists())
        folder_ready = bool(self._last_result_folder and self._last_result_folder.exists())
        enabled = not self._running
        self.open_video_button.configure(
            state=tk.NORMAL if enabled and video_ready else tk.DISABLED
        )
        self.open_folder_button.configure(
            state=tk.NORMAL if enabled and folder_ready else tk.DISABLED
        )

    def _open_last_video(self) -> None:
        if self._last_video_path:
            self._open_path(self._last_video_path)

    def _open_result_folder(self) -> None:
        if self._last_result_folder:
            self._open_path(self._last_result_folder)

    def _open_path(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
            if os.name == "nt":
                os.startfile(str(resolved))  # type: ignore[attr-defined]
            else:
                webbrowser.open(resolved.as_uri())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Pfad konnte nicht geöffnet werden", str(exc))

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self._running:
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "Verarbeitung läuft",
            "Verarbeitung abbrechen und das Fenster danach schließen?",
        ):
            return
        self._closing = True
        self._cancel()

    def _target_code(self) -> str:
        return self._code_from_display(self.target_language.get())

    def _tts_engine_code(self) -> str:
        return TTS_ENGINE_DISPLAY.get(self.tts_engine.get(), "edge")

    @staticmethod
    def _chatterbox_supports_language(language_code: str) -> bool:
        base_language = normalize_language_code(language_code).split("-", maxsplit=1)[0].casefold()
        return base_language in CHATTERBOX_LANGUAGES

    @staticmethod
    def _language_display(code: str) -> str:
        return f"{LANGUAGES.get(code, code)} [{code}]"

    @staticmethod
    def _code_from_display(display: str) -> str:
        value = display.strip()
        if value.endswith("]") and "[" in value:
            value = value.rsplit("[", maxsplit=1)[1][:-1]
        if not value:
            raise ValueError("Bitte einen Sprachcode auswählen oder eingeben.")
        return normalize_language_code(value)

    @staticmethod
    def _sorted_language_codes() -> list[str]:
        return sorted(LANGUAGES, key=lambda code: LANGUAGES[code].casefold())

    @staticmethod
    def _default_service_display() -> str:
        required = ("torch", "transformers", "sentencepiece")
        try:
            local_ready = all(importlib.util.find_spec(name) is not None for name in required)
        except (ImportError, AttributeError, ValueError):
            local_ready = False
        return "NLLB lokal · CPU/GPU" if local_ready else "Google Translate · online"


def run_gui() -> None:
    root = ttk.Window(themename="darkly")
    LinguoAIApp(root)
    root.mainloop()


def entrypoint() -> None:
    run_gui()
