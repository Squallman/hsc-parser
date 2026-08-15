"""Persistent browser management and debug artifact capture."""

from .diagnostics import Diagnostics, collect_interactive_elements
from .manager import BrowserManager

__all__ = ["BrowserManager", "Diagnostics", "collect_interactive_elements"]
