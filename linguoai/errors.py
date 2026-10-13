"""Domänenspezifische Fehler mit verständlichen Meldungen für GUI und CLI."""


class LinguoAIError(Exception):
    """Basisklasse für erwartbare Anwendungsfehler."""


class ConfigurationError(LinguoAIError):
    """Die Job-Konfiguration ist ungültig oder unvollständig."""


class DependencyError(LinguoAIError):
    """Ein benötigtes Programm oder Python-Paket fehlt."""


class ExternalToolError(LinguoAIError):
    """FFmpeg, FFprobe oder ein anderer externer Prozess ist fehlgeschlagen."""


class FileOperationError(LinguoAIError):
    """Ein-/Ausgabe oder die atomare Veröffentlichung ist fehlgeschlagen."""


class TranscriptionError(LinguoAIError):
    """Audio konnte nicht vollständig transkribiert werden."""


class TranslationError(LinguoAIError):
    """Text konnte nicht zuverlässig übersetzt werden."""


class SpeechSynthesisError(LinguoAIError):
    """Sprachausgabe konnte nicht erstellt oder eingepasst werden."""


class CancelledError(LinguoAIError):
    """Der Benutzer hat die Verarbeitung abgebrochen."""
