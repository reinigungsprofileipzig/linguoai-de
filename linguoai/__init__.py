"""LinguoAI: Video-Dubbing mit klar getrennten, testbaren Komponenten."""

from .config import AppConfig
from .domain import BatchResult, JobResult, Segment

__all__ = ["AppConfig", "BatchResult", "JobResult", "Segment"]
__version__ = "2.2.1"
