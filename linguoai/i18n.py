# ruff: noqa: RUF001
"""Dependency-free localization helpers shared by LinguoAI frontends.

German source strings are used as catalog keys.  This keeps migration of the
existing user interfaces small while still separating translated labels from
application state and provider codes.
"""

from __future__ import annotations

import json
import locale as system_locale
import os
import string
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

SUPPORTED_UI_LOCALES = ("de", "en", "ru", "uk", "tr", "es", "it")
DEFAULT_UI_LOCALE = "en"
UI_LANGUAGE_ENV = "LINGUOAI_UI_LANGUAGE"

_SETTINGS_FILE_NAME = "settings.json"
_SETTINGS_VERSION = 1
_MAX_SETTINGS_BYTES = 64 * 1024

_LOCALE_NAME_ALIASES = {
    "deutsch": "de",
    "german": "de",
    "deu": "de",
    "ger": "de",
    "english": "en",
    "englisch": "en",
    "eng": "en",
    "russian": "ru",
    "russisch": "ru",
    "rus": "ru",
    "ukrainian": "uk",
    "ukrainisch": "uk",
    "ukr": "uk",
    "turkish": "tr",
    "türkisch": "tr",
    "turkisch": "tr",
    "tur": "tr",
    "spanish": "es",
    "spanisch": "es",
    "spa": "es",
    "italian": "it",
    "italienisch": "it",
    "ita": "it",
}

# Each German source string has complete translations for the six non-German
# locales.  Catalogs are generated below and exposed as immutable mappings.
_TRANSLATIONS: dict[str, dict[str, str]] = {
    "Automatisch": {
        "en": "Automatic",
        "ru": "Автоматически",
        "uk": "Автоматично",
        "tr": "Otomatik",
        "es": "Automático",
        "it": "Automatico",
    },
    "Automatisch erkennen": {
        "en": "Detect automatically",
        "ru": "Определять автоматически",
        "uk": "Визначати автоматично",
        "tr": "Otomatik algıla",
        "es": "Detectar automáticamente",
        "it": "Rileva automaticamente",
    },
    "Automatisch (empfohlen)": {
        "en": "Automatic (recommended)",
        "ru": "Автоматически (рекомендуется)",
        "uk": "Автоматично (рекомендовано)",
        "tr": "Otomatik (önerilen)",
        "es": "Automático (recomendado)",
        "it": "Automatico (consigliato)",
    },
    "Bereit": {
        "en": "Ready",
        "ru": "Готово",
        "uk": "Готово",
        "tr": "Hazır",
        "es": "Listo",
        "it": "Pronto",
    },
    "Bereit für ein Video": {
        "en": "Ready for a video",
        "ru": "Готово к обработке видео",
        "uk": "Готово до обробки відео",
        "tr": "Video için hazır",
        "es": "Listo para procesar un vídeo",
        "it": "Pronto per elaborare un video",
    },
    "Start": {
        "en": "Start",
        "ru": "Начало",
        "uk": "Початок",
        "tr": "Başlangıç",
        "es": "Inicio",
        "it": "Avvio",
    },
    "Erweitert": {
        "en": "Advanced",
        "ru": "Дополнительно",
        "uk": "Розширені налаштування",
        "tr": "Gelişmiş",
        "es": "Avanzado",
        "it": "Avanzate",
    },
    "Protokoll": {
        "en": "Log",
        "ru": "Журнал",
        "uk": "Журнал",
        "tr": "Günlük",
        "es": "Registro",
        "it": "Registro",
    },
    "Video und Ausgabe": {
        "en": "Video and output",
        "ru": "Видео и результат",
        "uk": "Відео та результат",
        "tr": "Video ve çıktı",
        "es": "Vídeo y salida",
        "it": "Video e output",
    },
    "Quelle": {
        "en": "Source",
        "ru": "Источник",
        "uk": "Джерело",
        "tr": "Kaynak",
        "es": "Origen",
        "it": "Origine",
    },
    "Datei …": {
        "en": "File …",
        "ru": "Файл …",
        "uk": "Файл …",
        "tr": "Dosya …",
        "es": "Archivo …",
        "it": "File …",
    },
    "Ordner …": {
        "en": "Folder …",
        "ru": "Папка …",
        "uk": "Папка …",
        "tr": "Klasör …",
        "es": "Carpeta …",
        "it": "Cartella …",
    },
    "Ausgabe": {
        "en": "Output",
        "ru": "Результат",
        "uk": "Результат",
        "tr": "Çıktı",
        "es": "Salida",
        "it": "Output",
    },
    "Auswählen …": {
        "en": "Select …",
        "ru": "Выбрать …",
        "uk": "Вибрати …",
        "tr": "Seç …",
        "es": "Seleccionar …",
        "it": "Seleziona …",
    },
    "Sprache und Übersetzung": {
        "en": "Language and translation",
        "ru": "Язык и перевод",
        "uk": "Мова та переклад",
        "tr": "Dil ve çeviri",
        "es": "Idioma y traducción",
        "it": "Lingua e traduzione",
    },
    "Zielsprache": {
        "en": "Target language",
        "ru": "Язык перевода",
        "uk": "Цільова мова",
        "tr": "Hedef dil",
        "es": "Idioma de destino",
        "it": "Lingua di destinazione",
    },
    "Quellsprache": {
        "en": "Source language",
        "ru": "Исходный язык",
        "uk": "Мова оригіналу",
        "tr": "Kaynak dil",
        "es": "Idioma de origen",
        "it": "Lingua di origine",
    },
    "Übersetzungsart": {
        "en": "Translation method",
        "ru": "Способ перевода",
        "uk": "Спосіб перекладу",
        "tr": "Çeviri yöntemi",
        "es": "Método de traducción",
        "it": "Metodo di traduzione",
    },
    "Erkennung und Stimme": {
        "en": "Recognition and voice",
        "ru": "Распознавание и голос",
        "uk": "Розпізнавання та голос",
        "tr": "Tanıma ve ses",
        "es": "Reconocimiento y voz",
        "it": "Riconoscimento e voce",
    },
    "Spracherkennungsmodell": {
        "en": "Speech recognition model",
        "ru": "Модель распознавания речи",
        "uk": "Модель розпізнавання мовлення",
        "tr": "Konuşma tanıma modeli",
        "es": "Modelo de reconocimiento de voz",
        "it": "Modello di riconoscimento vocale",
    },
    "TTS-Engine": {
        "en": "TTS engine",
        "ru": "Модуль синтеза речи",
        "uk": "Система синтезу мовлення",
        "tr": "TTS motoru",
        "es": "Motor TTS",
        "it": "Motore TTS",
    },
    "Video verarbeiten": {
        "en": "Process video",
        "ru": "Обработать видео",
        "uk": "Обробити відео",
        "tr": "Videoyu işle",
        "es": "Procesar vídeo",
        "it": "Elabora video",
    },
    "Verarbeitung starten": {
        "en": "Start processing",
        "ru": "Начать обработку",
        "uk": "Почати обробку",
        "tr": "İşlemeyi başlat",
        "es": "Iniciar procesamiento",
        "it": "Avvia elaborazione",
    },
    "Abbrechen": {
        "en": "Cancel",
        "ru": "Отмена",
        "uk": "Скасувати",
        "tr": "İptal",
        "es": "Cancelar",
        "it": "Annulla",
    },
    "Status": {
        "en": "Status",
        "ru": "Статус",
        "uk": "Стан",
        "tr": "Durum",
        "es": "Estado",
        "it": "Stato",
    },
    "Ergebnisse herunterladen": {
        "en": "Download results",
        "ru": "Скачать результаты",
        "uk": "Завантажити результати",
        "tr": "Sonuçları indir",
        "es": "Descargar resultados",
        "it": "Scarica i risultati",
    },
    "Vorbereitung": {
        "en": "Preparing",
        "ru": "Подготовка",
        "uk": "Підготовка",
        "tr": "Hazırlık",
        "es": "Preparación",
        "it": "Preparazione",
    },
    "Audio extrahieren": {
        "en": "Extracting audio",
        "ru": "Извлечение аудио",
        "uk": "Вилучення аудіо",
        "tr": "Sesi çıkarma",
        "es": "Extrayendo audio",
        "it": "Estrazione audio",
    },
    "Sprache erkennen": {
        "en": "Recognizing speech",
        "ru": "Распознавание речи",
        "uk": "Розпізнавання мовлення",
        "tr": "Konuşmayı tanıma",
        "es": "Reconociendo voz",
        "it": "Riconoscimento vocale",
    },
    "Transkript optimieren": {
        "en": "Optimizing transcript",
        "ru": "Улучшение транскрипции",
        "uk": "Покращення транскрипції",
        "tr": "Transkripti iyileştirme",
        "es": "Optimizando transcripción",
        "it": "Ottimizzazione della trascrizione",
    },
    "Text übersetzen": {
        "en": "Translating text",
        "ru": "Перевод текста",
        "uk": "Переклад тексту",
        "tr": "Metni çevirme",
        "es": "Traduciendo texto",
        "it": "Traduzione del testo",
    },
    "Stimme erzeugen": {
        "en": "Generating voice",
        "ru": "Создание озвучки",
        "uk": "Створення озвучення",
        "tr": "Ses oluşturma",
        "es": "Generando voz",
        "it": "Generazione della voce",
    },
    "Tonspur synchronisieren": {
        "en": "Synchronizing audio track",
        "ru": "Синхронизация аудиодорожки",
        "uk": "Синхронізація аудіодоріжки",
        "tr": "Ses parçasını eşzamanlama",
        "es": "Sincronizando pista de audio",
        "it": "Sincronizzazione della traccia audio",
    },
    "Video fertigstellen": {
        "en": "Finalizing video",
        "ru": "Завершение видео",
        "uk": "Завершення відео",
        "tr": "Videoyu tamamlama",
        "es": "Finalizando vídeo",
        "it": "Finalizzazione del video",
    },
    "Abgeschlossen": {
        "en": "Completed",
        "ru": "Завершено",
        "uk": "Завершено",
        "tr": "Tamamlandı",
        "es": "Completado",
        "it": "Completato",
    },
    "Fehler": {
        "en": "Error",
        "ru": "Ошибка",
        "uk": "Помилка",
        "tr": "Hata",
        "es": "Error",
        "it": "Errore",
    },
    "Unerwarteter Fehler: {error}": {
        "en": "Unexpected error: {error}",
        "ru": "Непредвиденная ошибка: {error}",
        "uk": "Неочікувана помилка: {error}",
        "tr": "Beklenmeyen hata: {error}",
        "es": "Error inesperado: {error}",
        "it": "Errore imprevisto: {error}",
    },
    "Warnung: {warning}": {
        "en": "Warning: {warning}",
        "ru": "Предупреждение: {warning}",
        "uk": "Попередження: {warning}",
        "tr": "Uyarı: {warning}",
        "es": "Advertencia: {warning}",
        "it": "Avviso: {warning}",
    },
    "Erstellt: {path}": {
        "en": "Created: {path}",
        "ru": "Создано: {path}",
        "uk": "Створено: {path}",
        "tr": "Oluşturuldu: {path}",
        "es": "Creado: {path}",
        "it": "Creato: {path}",
    },
    "Untertitel: {path}": {
        "en": "Subtitles: {path}",
        "ru": "Субтитры: {path}",
        "uk": "Субтитри: {path}",
        "tr": "Altyazılar: {path}",
        "es": "Subtítulos: {path}",
        "it": "Sottotitoli: {path}",
    },
    "Fehlgeschlagen: {path}: {error}": {
        "en": "Failed: {path}: {error}",
        "ru": "Ошибка: {path}: {error}",
        "uk": "Не вдалося: {path}: {error}",
        "tr": "Başarısız: {path}: {error}",
        "es": "Falló: {path}: {error}",
        "it": "Non riuscito: {path}: {error}",
    },
    "Fertig: {name}": {
        "en": "Finished: {name}",
        "ru": "Готово: {name}",
        "uk": "Готово: {name}",
        "tr": "Tamamlandı: {name}",
        "es": "Finalizado: {name}",
        "it": "Completato: {name}",
    },
    "Erfolgreich: {completed} · Fehlgeschlagen: {failed}": {
        "en": "Successful: {completed} · Failed: {failed}",
        "ru": "Успешно: {completed} · Ошибки: {failed}",
        "uk": "Успішно: {completed} · Помилки: {failed}",
        "tr": "Başarılı: {completed} · Başarısız: {failed}",
        "es": "Correctos: {completed} · Fallidos: {failed}",
        "it": "Completati: {completed} · Non riusciti: {failed}",
    },
    "Abgebrochen.": {
        "en": "Cancelled.",
        "ru": "Отменено.",
        "uk": "Скасовано.",
        "tr": "İptal edildi.",
        "es": "Cancelado.",
        "it": "Annullato.",
    },
    "Verarbeitung wurde abgebrochen.": {
        "en": "Processing was cancelled.",
        "ru": "Обработка отменена.",
        "uk": "Обробку скасовано.",
        "tr": "İşleme iptal edildi.",
        "es": "Se canceló el procesamiento.",
        "it": "L'elaborazione è stata annullata.",
    },
    "Abbruch angefordert …": {
        "en": "Cancellation requested …",
        "ru": "Запрошена отмена …",
        "uk": "Надіслано запит на скасування …",
        "tr": "İptal istendi …",
        "es": "Cancelación solicitada …",
        "it": "Annullamento richiesto …",
    },
    "Verarbeitung fehlgeschlagen": {
        "en": "Processing failed",
        "ru": "Ошибка обработки",
        "uk": "Помилка обробки",
        "tr": "İşleme başarısız",
        "es": "Error de procesamiento",
        "it": "Elaborazione non riuscita",
    },
    "Einstellungen prüfen": {
        "en": "Check settings",
        "ru": "Проверьте настройки",
        "uk": "Перевірте налаштування",
        "tr": "Ayarları kontrol edin",
        "es": "Comprobar configuración",
        "it": "Controlla le impostazioni",
    },
    "Bitte einen Sprachcode auswählen oder eingeben.": {
        "en": "Select or enter a language code.",
        "ru": "Выберите или введите код языка.",
        "uk": "Виберіть або введіть код мови.",
        "tr": "Bir dil kodu seçin veya girin.",
        "es": "Selecciona o introduce un código de idioma.",
        "it": "Seleziona o inserisci un codice lingua.",
    },
    "Video direkt hochladen (hat Vorrang)": {
        "en": "Upload video directly (takes priority)",
        "ru": "Загрузить видео напрямую (имеет приоритет)",
        "uk": "Завантажити відео безпосередньо (має пріоритет)",
        "tr": "Videoyu doğrudan yükle (önceliklidir)",
        "es": "Subir vídeo directamente (tiene prioridad)",
        "it": "Carica direttamente il video (ha la priorità)",
    },
    "Alternativ: Pfad in MyDrive": {
        "en": "Alternatively: path in MyDrive",
        "ru": "Или укажите путь в MyDrive",
        "uk": "Або вкажіть шлях у MyDrive",
        "tr": "Alternatif: MyDrive yolu",
        "es": "Alternativa: ruta en MyDrive",
        "it": "In alternativa: percorso in MyDrive",
    },
    "Ergebnisse in MyDrive kopieren": {
        "en": "Copy results to MyDrive",
        "ru": "Копировать результаты в MyDrive",
        "uk": "Копіювати результати в MyDrive",
        "tr": "Sonuçları MyDrive'a kopyala",
        "es": "Copiar resultados a MyDrive",
        "it": "Copia i risultati in MyDrive",
    },
    "Helles Design": {
        "en": "Light theme",
        "ru": "Светлая тема",
        "uk": "Світла тема",
        "tr": "Açık tema",
        "es": "Tema claro",
        "it": "Tema chiaro",
    },
    "Dunkles Design": {
        "en": "Dark theme",
        "ru": "Тёмная тема",
        "uk": "Темна тема",
        "tr": "Koyu tema",
        "es": "Tema oscuro",
        "it": "Tema scuro",
    },
    "Deutsch": {
        "en": "German",
        "ru": "Немецкий",
        "uk": "Німецька",
        "tr": "Almanca",
        "es": "Alemán",
        "it": "Tedesco",
    },
    "Englisch": {
        "en": "English",
        "ru": "Английский",
        "uk": "Англійська",
        "tr": "İngilizce",
        "es": "Inglés",
        "it": "Inglese",
    },
    "Russisch": {
        "en": "Russian",
        "ru": "Русский",
        "uk": "Російська",
        "tr": "Rusça",
        "es": "Ruso",
        "it": "Russo",
    },
    "Ukrainisch": {
        "en": "Ukrainian",
        "ru": "Украинский",
        "uk": "Українська",
        "tr": "Ukraynaca",
        "es": "Ucraniano",
        "it": "Ucraino",
    },
    "Türkisch": {
        "en": "Turkish",
        "ru": "Турецкий",
        "uk": "Турецька",
        "tr": "Türkçe",
        "es": "Turco",
        "it": "Turco",
    },
    "Spanisch": {
        "en": "Spanish",
        "ru": "Испанский",
        "uk": "Іспанська",
        "tr": "İspanyolca",
        "es": "Español",
        "it": "Spagnolo",
    },
    "Italienisch": {
        "en": "Italian",
        "ru": "Итальянский",
        "uk": "Італійська",
        "tr": "İtalyanca",
        "es": "Italiano",
        "it": "Italiano",
    },
}


def _build_catalogs() -> Mapping[str, Mapping[str, str]]:
    catalogs: dict[str, Mapping[str, str]] = {}
    catalogs["de"] = MappingProxyType({source: source for source in _TRANSLATIONS})
    for locale_code in SUPPORTED_UI_LOCALES:
        if locale_code == "de":
            continue
        catalog = {
            source: translations[locale_code]
            for source, translations in _TRANSLATIONS.items()
            if locale_code in translations
        }
        catalogs[locale_code] = MappingProxyType(catalog)
    return MappingProxyType(catalogs)


CATALOGS = _build_catalogs()


def _match_supported_locale(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    candidate = candidate.split(".", maxsplit=1)[0].split("@", maxsplit=1)[0]
    folded = candidate.casefold().replace("-", "_")
    if folded in {"auto", "c", "posix", "system", "default"}:
        return None
    base = folded.split("_", maxsplit=1)[0]
    base = _LOCALE_NAME_ALIASES.get(base, base)
    return base if base in SUPPORTED_UI_LOCALES else None


def normalize_ui_locale(value: str | None, *, fallback: str = DEFAULT_UI_LOCALE) -> str:
    """Return a supported base locale, falling back to English by default."""

    matched = _match_supported_locale(value)
    if matched:
        return matched
    return _match_supported_locale(fallback) or DEFAULT_UI_LOCALE


def _system_locale_name() -> str | None:
    try:
        locale_name = system_locale.getlocale()[0]
    except (TypeError, ValueError):
        locale_name = None
    if locale_name:
        return locale_name
    try:
        queried = system_locale.setlocale(system_locale.LC_CTYPE)
    except (TypeError, ValueError, OSError):
        return None
    return queried or None


def detect_ui_locale(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    os_locale: str | None = None,
) -> str:
    """Detect the UI locale using explicit, environment, OS, then English."""

    environment = os.environ if environ is None else environ
    candidates = (
        explicit,
        environment.get(UI_LANGUAGE_ENV),
        os_locale if os_locale is not None else _system_locale_name(),
    )
    for candidate in candidates:
        matched = _match_supported_locale(candidate)
        if matched:
            return matched
    return DEFAULT_UI_LOCALE


def get_user_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Path:
    """Return the per-user settings path without creating any directories."""

    environment = os.environ if environ is None else environ
    custom_root = environment.get("LINGUOAI_CONFIG_HOME", "").strip()
    if custom_root:
        return Path(custom_root).expanduser() / _SETTINGS_FILE_NAME

    user_home = Path(home).expanduser() if home is not None else Path.home()
    if sys.platform == "win32":
        app_data = environment.get("APPDATA", "").strip()
        root = Path(app_data).expanduser() if app_data else user_home / "AppData" / "Roaming"
        return root / "LinguoAI" / _SETTINGS_FILE_NAME
    if sys.platform == "darwin":
        return user_home / "Library" / "Application Support" / "LinguoAI" / _SETTINGS_FILE_NAME

    xdg_root = environment.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg_root).expanduser() if xdg_root else user_home / ".config"
    return root / "linguoai" / _SETTINGS_FILE_NAME


def _read_settings(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_saved_ui_locale(path: str | Path | None = None) -> str | None:
    """Load a valid saved UI locale; malformed or inaccessible files yield None."""

    settings_path = Path(path).expanduser() if path is not None else get_user_config_path()
    settings = _read_settings(settings_path)
    if settings is None:
        return None
    return _match_supported_locale(settings.get("ui_language"))


def save_ui_locale(locale_code: str, path: str | Path | None = None) -> bool:
    """Atomically persist a supported locale and preserve unrelated JSON settings."""

    normalized = _match_supported_locale(locale_code)
    if normalized is None:
        return False

    settings_path = Path(path).expanduser() if path is not None else get_user_config_path()
    temporary_path: Path | None = None
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = _read_settings(settings_path) or {}
        settings["version"] = _SETTINGS_VERSION
        settings["ui_language"] = normalized
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{settings_path.name}.",
            suffix=".tmp",
            dir=settings_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(settings, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, settings_path)
        temporary_path = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def placeholder_names(template: str) -> frozenset[str]:
    """Return named format fields from a catalog string.

    Invalid brace syntax raises ``ValueError`` so catalog validation can report
    a broken translation before it reaches a user.
    """

    names: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if field_name is not None:
            names.add(field_name)
    return frozenset(names)


def catalog_placeholder_errors(
    catalogs: Mapping[str, Mapping[str, str]] = CATALOGS,
) -> tuple[str, ...]:
    """Return deterministic diagnostics for malformed or mismatched placeholders."""

    errors: list[str] = []
    for locale_code in sorted(catalogs):
        for source, translation in sorted(catalogs[locale_code].items()):
            try:
                source_fields = placeholder_names(source)
            except ValueError as exc:
                errors.append(f"{locale_code}: invalid source {source!r}: {exc}")
                continue
            try:
                translated_fields = placeholder_names(translation)
            except ValueError as exc:
                errors.append(f"{locale_code}: invalid translation for {source!r}: {exc}")
                continue
            if source_fields != translated_fields:
                errors.append(
                    f"{locale_code}: placeholders for {source!r}: "
                    f"{sorted(source_fields)!r} != {sorted(translated_fields)!r}"
                )
    return tuple(errors)


def _render(template: str, params: Mapping[str, Any]) -> str | None:
    if not params:
        return template
    try:
        required = placeholder_names(template)
    except ValueError:
        return None
    if not required.issubset(params):
        return None
    try:
        return template.format_map(params)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Translator:
    """Immutable locale-bound translator with English and German fallbacks."""

    locale: str = DEFAULT_UI_LOCALE

    def __post_init__(self) -> None:
        object.__setattr__(self, "locale", normalize_ui_locale(self.locale))

    def gettext(self, source: str, **params: Any) -> str:
        primary = CATALOGS[self.locale].get(source)
        if self.locale == "de":
            primary = source
        candidates = (primary, CATALOGS["en"].get(source), source)
        first = next((candidate for candidate in candidates if candidate is not None), source)
        for candidate in candidates:
            if candidate is None:
                continue
            rendered = _render(candidate, params)
            if rendered is not None:
                return rendered
        return first

    __call__ = gettext
